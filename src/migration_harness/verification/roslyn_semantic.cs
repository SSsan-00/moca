using System;
using System.Collections.Generic;
using System.Collections.Immutable;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.AspNetCore.Mvc.Razor.Extensions;
using Microsoft.AspNetCore.Razor.Language;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

internal static partial class Program
{
    private sealed record ProjectContext(
        Target[] Sources, Target[] Imports, string ProjectPath, string[] References, bool Web,
        string AssemblyName, string RootNamespace, bool ImplicitUsings, string OutputType, string Nullable);

    private sealed record SemanticRequest(Target Target, ProjectContext Project);

    private static readonly string[] CoreUsings = [
        "System", "System.Collections.Generic", "System.IO", "System.Linq", "System.Net.Http",
        "System.Threading", "System.Threading.Tasks"];
    private static readonly string[] WebUsings = [
        "System.Net.Http.Json", "Microsoft.AspNetCore.Builder", "Microsoft.AspNetCore.Hosting",
        "Microsoft.AspNetCore.Http", "Microsoft.AspNetCore.Routing", "Microsoft.Extensions.Configuration",
        "Microsoft.Extensions.DependencyInjection", "Microsoft.Extensions.Hosting", "Microsoft.Extensions.Logging"];
    private static readonly HashSet<string> GeneratorAttributes = new(StringComparer.Ordinal)
    {
        "GeneratedRegex", "JsonSerializable", "LoggerMessage", "GeneratedComInterface", "GeneratedComClass", "LibraryImport"
    };

    private static int RunSemantic(string payload)
    {
        var request = JsonSerializer.Deserialize<SemanticRequest>(payload);
        if (request is null) return 2;
        var (target, context) = request;
        Console.WriteLine(JsonSerializer.Serialize(SemanticDiagnostics(target, context)));
        return 0;
    }

    private static object SemanticDiagnostics(Target target, ProjectContext context)
    {
        var findings = new List<object>();
        object Result(string? reason = null, bool truncated = false) => new
        {
            findings, skipped = reason is null ? [] : new[] {new {path = target.Path, reason}},
            truncated, razor = Array.Empty<object>()
        };

        var options = new CSharpParseOptions(LanguageVersion.CSharp13);
        SyntaxTree Parse(Target input) => CSharpSyntaxTree.ParseText(input.Text!, options, input.Path);

        var sources = context.Sources.Select(Parse).ToList();
        RazorCSharpDocument? razor = null;
        SyntaxTree subject;
        var original = SourceText.From(target.Text!);
        if (target.Path.EndsWith(".cshtml", StringComparison.OrdinalIgnoreCase))
        {
            var source = RazorSourceDocument.ReadFrom(new MemoryItem(context.ProjectPath, target.Text!));
            var imports = context.Imports.Select(item => RazorSourceDocument.ReadFrom(new MemoryItem(item.Path, item.Text!))).ToImmutableArray();
            if (context.Imports.Append(target).Any(UsesUnknownTagHelpers))
                return Result("razor_context");
            var engine = RazorProjectEngine.Create(RazorConfiguration.Default,
                RazorProjectFileSystem.Create(Directory.GetCurrentDirectory()), builder =>
                {
                    RazorExtensions.Register(builder);
                    builder.SetRootNamespace(context.RootNamespace);
                });
            razor = engine.Process(source, FileKinds.Legacy, imports, []).GetCSharpDocument();
            if (razor.Diagnostics.Any(d => d.Severity == RazorDiagnosticSeverity.Error))
                return Result("razor_context");
            subject = CSharpSyntaxTree.ParseText(razor.Text, options, context.ProjectPath + ".g.cs");
        }
        else
        {
            subject = Parse(target);
        }
        sources.Add(subject);
        if (sources.Any(RequiresProjectEvaluation))
            return Result("project_configuration");

        if (context.ImplicitUsings)
        {
            var namespaces = context.Web ? CoreUsings.Concat(WebUsings) : CoreUsings;
            sources.Add(CSharpSyntaxTree.ParseText(string.Join("\n", namespaces.Select(ns => $"global using {ns};")), options));
        }
        var nullable = context.Nullable switch
        {
            "enable" => NullableContextOptions.Enable,
            "annotations" => NullableContextOptions.Annotations,
            "warnings" => NullableContextOptions.Warnings,
            _ => NullableContextOptions.Disable
        };
        var output = context.OutputType switch
        {
            "Exe" => OutputKind.ConsoleApplication,
            "WinExe" => OutputKind.WindowsApplication,
            _ => OutputKind.DynamicallyLinkedLibrary
        };
        var references = context.References.Select(path => MetadataReference.CreateFromFile(path));
        var compilation = CSharpCompilation.Create(context.AssemblyName, sources, references,
            new CSharpCompilationOptions(output, nullableContextOptions: nullable));
        var unmapped = false;
        foreach (var diagnostic in compilation.GetSemanticModel(subject).GetDiagnostics())
        {
            if (diagnostic.Severity != DiagnosticSeverity.Error || diagnostic.Location.SourceTree != subject)
                continue;
            var span = diagnostic.Location.SourceSpan;
            if (razor is not null && !TryMapRazorSpan(razor, subject.GetText(), original, span, out span))
            {
                unmapped = true;
                continue;
            }
            if (findings.Count == MaxFindings)
                return Result(unmapped ? "razor_unmapped_errors" : null, truncated: true);
            var position = original.Lines.GetLinePosition(span.Start);
            findings.Add(new
            {
                path = target.Path, line = position.Line + 1, column = position.Character + 1,
                start = span.Start, length = span.Length, code = diagnostic.Id,
                message = diagnostic.GetMessage(CultureInfo.InvariantCulture)
            });
        }
        return Result(unmapped ? "razor_unmapped_errors" : null);
    }

    private static bool RequiresProjectEvaluation(SyntaxTree tree)
    {
        var root = tree.GetRoot();
        return root.DescendantTrivia(descendIntoTrivia: true).Any(t =>
                   t.IsKind(SyntaxKind.IfDirectiveTrivia) || t.IsKind(SyntaxKind.ErrorDirectiveTrivia))
            || root.DescendantNodes().OfType<AttributeSyntax>().Any(attribute =>
            {
                var name = attribute.Name switch
                {
                    SimpleNameSyntax simple => simple.Identifier.ValueText,
                    QualifiedNameSyntax qualified => qualified.Right.Identifier.ValueText,
                    AliasQualifiedNameSyntax alias => alias.Name.Identifier.ValueText,
                    _ => ""
                };
                return GeneratorAttributes.Contains(name.EndsWith("Attribute", StringComparison.Ordinal) ? name[..^9] : name)
                    || attribute.Parent?.Parent is MemberDeclarationSyntax member
                        && member.Modifiers.Any(SyntaxKind.PartialKeyword);
            });
    }

    private static bool UsesUnknownTagHelpers(Target target) => target.Text!.Split('\n').Any(line =>
    {
        var directive = line.Trim();
        return (directive.StartsWith("@addTagHelper", StringComparison.Ordinal) &&
                (directive["@addTagHelper".Length..].Split(',') is not [var name, var assembly] ||
                 name.Trim() != "*" || assembly.Trim() != "Microsoft.AspNetCore.Mvc.TagHelpers"))
            || directive.StartsWith("@tagHelperPrefix", StringComparison.Ordinal);
    });

    private static bool TryMapRazorSpan(RazorCSharpDocument doc, SourceText generated, SourceText original,
        TextSpan span, out TextSpan sourceSpan)
    {
        sourceSpan = default;
        foreach (var mapping in doc.SourceMappings)
        {
            var from = mapping.GeneratedSpan;
            var to = mapping.OriginalSpan;
            if (!string.Equals(to.FilePath, doc.CodeDocument.Source.FilePath, StringComparison.OrdinalIgnoreCase)
                || from.Length != to.Length || span.IsEmpty || span.Start < from.AbsoluteIndex
                || span.End > from.AbsoluteIndex + from.Length)
                continue;
            var mapped = new TextSpan(to.AbsoluteIndex + span.Start - from.AbsoluteIndex, span.Length);
            if (mapped.End > original.Length || generated.ToString(span) != original.ToString(mapped))
                return false;
            sourceSpan = mapped;
            return true;
        }
        return false;
    }

}

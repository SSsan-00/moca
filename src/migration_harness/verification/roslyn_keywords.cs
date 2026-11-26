using System;
using System.Collections.Generic;
using System.Diagnostics.CodeAnalysis;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Diagnostics;
using Microsoft.CodeAnalysis.Text;
using Microsoft.NET.Sdk.Razor.SourceGenerators;

internal static class Program
{
    private sealed record Target(string Path, string Text);
    private sealed record Finding(string text, int line, int column);

    private static readonly CSharpParseOptions Options = new(LanguageVersion.Preview, kind: SourceCodeKind.Regular);

    private static int Main(string[] args)
    {
        if (args.Length != 1 || args[0] != "--escaped-keywords") return 2;
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.InputEncoding = new UTF8Encoding(false, true);
        var targets = JsonSerializer.Deserialize<Target[]>(Console.In.ReadToEnd()) ?? [];
        var findings = targets.Select(_ => new List<Finding>()).ToArray();
        var razor = new List<RazorInput>();
        for (var index = 0; index < targets.Length; index++)
        {
            var target = targets[index];
            var source = SourceText.From(target.Text, Encoding.UTF8);
            if (target.Path.EndsWith(".cshtml", StringComparison.OrdinalIgnoreCase))
                razor.Add(new(index, source));
            else
                foreach (var token in Declarations(CSharpSyntaxTree.ParseText(source, Options)))
                    findings[index].Add(At(token, source, source.Lines.GetLinePosition(token.SpanStart)));
        }
        if (razor.Count > 0 && !AnalyzeRazor(razor, findings)) return 3;
        Console.WriteLine(JsonSerializer.Serialize(findings));
        return 0;
    }

    private static bool AnalyzeRazor(List<RazorInput> inputs, List<Finding>[] findings)
    {
        var paths = inputs.ToDictionary(input => input.Path, StringComparer.OrdinalIgnoreCase);
        var driver = CSharpGeneratorDriver.Create(
            [new RazorSourceGenerator().AsSourceGenerator()], inputs, Options, new GeneratorOptions());
        var generated = driver.RunGenerators(CSharpCompilation.Create("KeywordGuard")).GetRunResult().GeneratedTrees;
        if (generated.Length != inputs.Count) return false;
        foreach (var tree in generated)
        {
            foreach (var token in Declarations(tree))
            {
                var span = tree.GetMappedLineSpan(token.Span);
                if (!span.HasMappedPath || !paths.TryGetValue(Path.GetFullPath(span.Path), out var input)
                    || span.StartLinePosition.Line < 0 || span.StartLinePosition.Line >= input.Source.Lines.Count) return false;
                var line = input.Source.Lines[span.StartLinePosition.Line];
                var column = span.StartLinePosition.Character;
                // A user #line can override Razor's map. Never trust a position without the original token.
                if (column < 0 || column + token.Text.Length > line.Span.Length
                    || input.Source.ToString(new TextSpan(line.Start + column, token.Text.Length)) != token.Text)
                    return false;
                findings[input.Index].Add(At(token, input.Source, span.StartLinePosition));
            }
        }
        return true;
    }

    private static Finding At(SyntaxToken token, SourceText source, LinePosition at)
    {
        var prefix = source.ToString(new TextSpan(source.Lines[at.Line].Start, at.Character));
        return new Finding(token.Text, at.Line, prefix.EnumerateRunes().Count());
    }

    private static IEnumerable<SyntaxToken> Declarations(SyntaxTree tree) => tree.GetRoot().DescendantTokens()
        .Where(token => token.IsKind(SyntaxKind.IdentifierToken) && token.Text.StartsWith('@')
                        && SyntaxFacts.GetKeywordKind(token.ValueText) != SyntaxKind.None && IsDeclaration(token));

    private static bool IsDeclaration(SyntaxToken token) => token.Parent switch
    {
        VariableDeclaratorSyntax node => token == node.Identifier,
        ParameterSyntax node => token == node.Identifier,
        PropertyDeclarationSyntax node => token == node.Identifier && node.ExplicitInterfaceSpecifier is null
                                          && !node.Modifiers.Any(SyntaxKind.OverrideKeyword),
        EventDeclarationSyntax node => token == node.Identifier && node.ExplicitInterfaceSpecifier is null
                                       && !node.Modifiers.Any(SyntaxKind.OverrideKeyword),
        MethodDeclarationSyntax node => token == node.Identifier && node.ExplicitInterfaceSpecifier is null
                                        && !node.Modifiers.Any(SyntaxKind.OverrideKeyword),
        LocalFunctionStatementSyntax node => token == node.Identifier,
        BaseTypeDeclarationSyntax node => token == node.Identifier,
        DelegateDeclarationSyntax node => token == node.Identifier,
        EnumMemberDeclarationSyntax node => token == node.Identifier,
        TypeParameterSyntax node => token == node.Identifier,
        SingleVariableDesignationSyntax node => token == node.Identifier,
        ForEachStatementSyntax node => token == node.Identifier,
        CatchDeclarationSyntax node => token == node.Identifier,
        FromClauseSyntax node => token == node.Identifier,
        LetClauseSyntax node => token == node.Identifier,
        JoinClauseSyntax node => token == node.Identifier,
        JoinIntoClauseSyntax node => token == node.Identifier,
        QueryContinuationSyntax node => token == node.Identifier,
        _ => false,
    };

    private sealed class RazorInput(int index, SourceText source) : AdditionalText
    {
        public int Index => index;
        public SourceText Source => source;
        public override string Path { get; } = System.IO.Path.GetFullPath($"Page{index}.cshtml");
        public override SourceText GetText(CancellationToken cancellationToken) => Source;
    }

    private sealed class GeneratorOptions : AnalyzerConfigOptionsProvider
    {
        public override AnalyzerConfigOptions GlobalOptions => new Values(new()
        {
            ["build_property.RazorLangVersion"] = "9.0",
            ["build_property.RootNamespace"] = "KeywordGuard",
            ["build_property.MSBuildProjectDirectory"] = Directory.GetCurrentDirectory(),
        });
        public override AnalyzerConfigOptions GetOptions(SyntaxTree tree) => new Values(new());
        public override AnalyzerConfigOptions GetOptions(AdditionalText text) => new Values(new()
        {
            ["build_metadata.AdditionalFiles.TargetPath"] = Convert.ToBase64String(
                Encoding.UTF8.GetBytes(Path.GetFileName(text.Path))),
        });
    }

    private sealed class Values(Dictionary<string, string> values) : AnalyzerConfigOptions
    {
        public override bool TryGetValue(string key, [NotNullWhen(true)] out string? value) =>
            values.TryGetValue(key, out value);
    }
}

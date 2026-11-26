using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;
using Microsoft.AspNetCore.Mvc.Razor.Extensions;
using Microsoft.AspNetCore.Razor.Language;

internal static partial class Program
{
    private sealed record Target(string Path, string? Text);

    private const int MaxFindings = 40;
    private static readonly CSharpParseOptions Options = new(LanguageVersion.Preview, kind: SourceCodeKind.Regular);

    private static int Main(string[] args)
    {
        if (args.Length != 1) return 2;
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.InputEncoding = new UTF8Encoding(false, true);
        if (args[0] == "--semantic") return RunSemantic(Console.In.ReadToEnd());
        if (args[0] == "--semantic-stream")
        {
            while (Console.ReadLine() is { } payload)
                if (RunSemantic(payload) != 0) return 2;
            return 0;
        }
        var targets = args[0] == "--stdin"
            ? JsonSerializer.Deserialize<Target[]>(Console.In.ReadToEnd()) ?? []
            : (JsonSerializer.Deserialize<string[]>(File.ReadAllText(args[0])) ?? [])
                .Select(path => new Target(path, null)).ToArray();
        var findings = new List<object>();
        var skipped = new List<object>();
        var razor = new List<object>();
        var truncated = false;
        foreach (var target in targets)
        {
            var path = target.Path;
            try
            {
                var source = target.Text ?? File.ReadAllText(path, new UTF8Encoding(false, true));
                if (path.EndsWith(".cshtml", StringComparison.OrdinalIgnoreCase))
                {
                    truncated |= RazorDiagnostics(path, source, razor);
                    continue;
                }
                if (!path.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
                {
                    skipped.Add(new { path, reason = "unsupported_file_type" });
                    continue;
                }
                var tree = CSharpSyntaxTree.ParseText(source, Options, path);
                if (tree.GetRoot().DescendantTrivia(descendIntoTrivia: true)
                    .Any(trivia => trivia.IsKind(SyntaxKind.IfDirectiveTrivia)
                        || trivia.IsKind(SyntaxKind.ErrorDirectiveTrivia)))
                {
                    skipped.Add(new { path, reason = "project_preprocessor_required" });
                    continue;
                }

                foreach (var diagnostic in tree.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error))
                {
                    if (findings.Count == MaxFindings) { truncated = true; break; }
                    var start = diagnostic.Location.GetLineSpan().StartLinePosition;
                    findings.Add(new
                    {
                        path, line = start.Line + 1, column = start.Character + 1,
                        start = diagnostic.Location.SourceSpan.Start, length = diagnostic.Location.SourceSpan.Length,
                        code = diagnostic.Id, message = diagnostic.GetMessage(CultureInfo.InvariantCulture)
                    });
                }
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or DecoderFallbackException)
            {
                skipped.Add(new { path, reason = "unreadable_source" });
            }
        }
        Console.WriteLine(JsonSerializer.Serialize(new { findings, skipped, truncated, razor }));
        return 0;
    }
    private static RazorCSharpDocument RazorCode(string path, string source)
    {
        var engine = RazorProjectEngine.Create(RazorConfiguration.Default,
            RazorProjectFileSystem.Create(Directory.GetCurrentDirectory()), builder => RazorExtensions.Register(builder));
        return engine.Process(RazorSourceDocument.ReadFrom(new MemoryItem(Path.GetFileName(path), source)),
            FileKinds.Legacy, [], []).GetCSharpDocument();
    }

    private static bool RazorDiagnostics(string path, string source, List<object> findings)
    {
        foreach (var diagnostic in RazorCode(path, source).Diagnostics)
        {
            var span = diagnostic.Span;
            // Without project imports/tag-helper metadata, only unmatched Razor delimiters are certain.
            if (diagnostic.Severity != RazorDiagnosticSeverity.Error || diagnostic.Id != "RZ1006"
                || span.AbsoluteIndex < 0 || span.Length < 0) continue;
            if (findings.Count == MaxFindings) return true;
            findings.Add(new
            {
                path, line = span.LineIndex + 1, column = span.CharacterIndex + 1,
                start = span.AbsoluteIndex, length = span.Length, code = diagnostic.Id, message = diagnostic.GetMessage()
            });
        }
        return false;
    }

    private sealed class MemoryItem(string path, string source) : RazorProjectItem
    {
        public override string BasePath => "/";
        public override string FilePath => "/" + path;
        public override string PhysicalPath => Path.GetFullPath(path);
        public override string RelativePhysicalPath => path;
        public override bool Exists => true;
        public override Stream Read() => new MemoryStream(Encoding.UTF8.GetBytes(source));
    }

}

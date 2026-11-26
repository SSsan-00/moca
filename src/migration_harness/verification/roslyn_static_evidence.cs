using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Microsoft.AspNetCore.Razor.Language;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

internal static partial class Program
{
    // Bounded transport: the checker must never claim to have seen everything after a cutoff.
    private const int StaticFactLimit = 4_000;
    private const int StaticValueLimit = 4_096;
    private const int StaticValueBudget = 200_000;

    private sealed record StaticFact(string kind, string value, int start_line, int end_line);
    private sealed record StaticReport(int index, string path, bool complete, string reason, List<StaticFact> facts);

    private static int ReportStaticEvidence(string payload)
    {
        var targets = JsonSerializer.Deserialize<Target[]>(payload) ?? [];
        var remainingFacts = StaticFactLimit;
        var remainingValues = StaticValueBudget;
        var reports = new List<StaticReport>(targets.Length);
        for (var index = 0; index < targets.Length; index++)
            reports.Add(StaticEvidence(targets[index], index, ref remainingFacts, ref remainingValues));
        Console.WriteLine(JsonSerializer.Serialize(reports));
        return 0;
    }

    private static StaticReport StaticEvidence(Target target, int index, ref int remainingFacts, ref int remainingValues)
    {
        var facts = new List<StaticFact>();
        StaticReport Report(string reason) => new(index, target.Path, reason == "ok", reason, facts);

        var isRazor = target.Path.EndsWith(".cshtml", StringComparison.OrdinalIgnoreCase);
        if (!isRazor && !target.Path.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
            return Report("unsupported_file_type");
        if (string.IsNullOrWhiteSpace(target.Text))
            return Report("empty_target");

        var original = SourceText.From(target.Text);
        var razor = isRazor ? RazorCode(target.Path, target.Text) : null;
        var generated = razor is null ? original : GetRazorGeneratedText(razor);
        var tree = CSharpSyntaxTree.ParseText(generated, Options, target.Path);
        var root = tree.GetRoot();
        var reason = razor is not null && razor.Diagnostics.Any(d => d.Severity == RazorDiagnosticSeverity.Error)
            ? "razor_parse_errors"
            : root.DescendantTrivia(descendIntoTrivia: true).Any(t =>
                t.IsKind(SyntaxKind.IfDirectiveTrivia) || t.IsKind(SyntaxKind.ErrorDirectiveTrivia))
                ? "project_preprocessor_required"
                : tree.GetDiagnostics().Any(d => d.Severity == DiagnosticSeverity.Error) ? "syntax_errors"
                // Missing project imports and generated markup/directives are not evidence of absence.
                : razor is not null ? "razor_context_unverified" : "ok";

        foreach (var node in root.DescendantNodes())
        {
            if (StaticEvidenceToken(node) is not { } candidate || candidate.Token.IsMissing)
                continue;
            var span = candidate.Token.Span;
            if (razor is not null && !TryMapRazorSpan(razor, generated, original, span, out span))
                continue;
            var value = candidate.Token.ValueText;
            if (value.Length > StaticValueLimit || value.Length > remainingValues || remainingFacts == 0)
            {
                if (reason is "ok" or "razor_context_unverified") reason = "fact_limit";
                if (remainingFacts == 0 || remainingValues == 0) break;
                continue;
            }

            remainingFacts--;
            remainingValues -= value.Length;
            facts.Add(new StaticFact(candidate.Kind, value,
                original.Lines.GetLineFromPosition(span.Start).LineNumber + 1,
                original.Lines.GetLineFromPosition(span.End - 1).LineNumber + 1));
        }
        return Report(reason);
    }

    private static (string Kind, SyntaxToken Token)? StaticEvidenceToken(SyntaxNode node) => node switch
    {
        BaseTypeDeclarationSyntax declaration => ("type", declaration.Identifier),
        DelegateDeclarationSyntax declaration => ("type", declaration.Identifier),
        MethodDeclarationSyntax declaration => ("method", declaration.Identifier),
        LocalFunctionStatementSyntax declaration => ("method", declaration.Identifier),
        PropertyDeclarationSyntax declaration => ("property", declaration.Identifier),
        VariableDeclaratorSyntax v when v.Parent?.Parent is FieldDeclarationSyntax field
            && field.Modifiers.Any(SyntaxKind.ConstKeyword) => ("const", v.Identifier),
        VariableDeclaratorSyntax v when v.Parent?.Parent is LocalDeclarationStatementSyntax local
            && local.Modifiers.Any(SyntaxKind.ConstKeyword) => ("const", v.Identifier),
        EnumMemberDeclarationSyntax declaration => ("const", declaration.Identifier),
        LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.StringLiteralExpression)
            => ("literal", literal.Token),
        _ => null,
    };
}

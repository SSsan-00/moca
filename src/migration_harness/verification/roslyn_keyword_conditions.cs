using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

internal static partial class Program
{
    private const int KeywordVariantLimit = 16;
    private const int KeywordConditionBudget = 512;

    private sealed record Branch(int Directive, int Start, int End, (ExpressionSyntax Expression, bool Expected)[] Conditions);
    private sealed record OpenBranch(int Directive, int Start, (ExpressionSyntax Expression, bool Expected)[] Previous,
                                     ExpressionSyntax? Condition);

    private static (List<SyntaxNode> Roots, int? Unchecked) KeywordConditionRoots(SourceText source)
    {
        var initial = CSharpSyntaxTree.ParseText(source, Options).GetRoot();
        var roots = new List<SyntaxNode> { initial };
        if (!initial.ContainsDirectives) return (roots, null);
        var directives = initial.DescendantTrivia().Select(trivia => trivia.GetStructure())
            .OfType<DirectiveTriviaSyntax>().ToArray();
        if (!directives.Any(d => d is IfDirectiveTriviaSyntax)) return (roots, null);
        var candidates = initial.DescendantTokens().Concat(initial.DescendantTrivia().Where(IsUnparsedTrivia)
            .SelectMany(trivia => SyntaxFactory.ParseTokens(trivia.ToFullString(),
                initialTokenPosition: trivia.FullSpan.Start, options: Options)))
            .Where(IsEscapedReservedKeyword).Select(token => token.SpanStart).Distinct().Order().ToArray();
        if (candidates.Length == 0) return (roots, null);
        if (directives.Length > 512) return (roots, candidates[0]);
        var pending = candidates.Where(pos => IsUnparsed(initial, pos)).ToHashSet();
        var seen = new HashSet<string>(StringComparer.Ordinal) { "" };
        var budget = KeywordConditionBudget;
        var branches = ConditionalBranches(directives)
            .OrderByDescending(branch => candidates.Any(pos => pos >= branch.Start && pos < branch.End)).ToArray();
        var impossible = new List<Branch>();
        var localSymbolChanges = directives.Select(directive => directive switch
        {
            DefineDirectiveTriviaSyntax define => define.Name.ValueText,
            UndefDirectiveTriviaSyntax undef => undef.Name.ValueText,
            _ => null,
        }).OfType<string>().ToHashSet(StringComparer.Ordinal);
        foreach (var branch in branches)
        {
            if (roots.Count == KeywordVariantLimit || budget <= 0) break;
            if (roots.Any(root => root.FindTrivia(branch.Directive).GetStructure() is
                    BranchingDirectiveTriviaSyntax { IsActive: true, BranchTaken: true })) continue;
            if (branch.Conditions.Any(condition => condition.Expression.ContainsDiagnostics)) continue;
            var known = new Dictionary<string, bool>(StringComparer.Ordinal);
            var result = FindBranchSymbols(branch.Conditions, known, ref budget);
            if (result == false && !branch.Conditions.SelectMany(condition => condition.Expression.DescendantNodesAndSelf())
                .OfType<IdentifierNameSyntax>().Any(name => localSymbolChanges.Contains(name.Identifier.ValueText)))
                impossible.Add(branch);
            if (result != true) continue;
            var symbols = known.Where(pair => pair.Value).Select(pair => pair.Key).Order(StringComparer.Ordinal).ToArray();
            if (!seen.Add(string.Join(" ", symbols))) continue;
            var root = CSharpSyntaxTree.ParseText(source, Options.WithPreprocessorSymbols(symbols)).GetRoot();
            roots.Add(root);
            pending.RemoveWhere(position => !IsUnparsed(root, position));
        }
        pending.RemoveWhere(position => impossible.Any(branch => branch.Start <= position && position < branch.End));
        return (roots, pending.Count == 0 ? null : pending.Min());
    }

    private static bool IsUnparsed(SyntaxNode root, int position) => IsUnparsedTrivia(root.FindTrivia(position));

    private static bool IsUnparsedTrivia(SyntaxTrivia trivia) => trivia.Kind() is
        SyntaxKind.DisabledTextTrivia or SyntaxKind.SkippedTokensTrivia;

    private static IEnumerable<Branch> ConditionalBranches(IEnumerable<DirectiveTriviaSyntax> directives)
    {
        var open = new Stack<OpenBranch>();
        foreach (var directive in directives)
        {
            if (directive is IfDirectiveTriviaSyntax start)
            {
                open.Push(new(start.SpanStart, start.FullSpan.End, open.TryPeek(out var parent) ? WithBranch(parent) : [], start.Condition));
            }
            else if (open.Count > 0 && directive is ElifDirectiveTriviaSyntax or ElseDirectiveTriviaSyntax
                     or EndIfDirectiveTriviaSyntax)
            {
                var old = open.Pop();
                yield return new Branch(old.Directive, old.Start, directive.FullSpan.Start, WithBranch(old));
                if (directive is not EndIfDirectiveTriviaSyntax)
                    open.Push(new(directive.SpanStart, directive.FullSpan.End, old.Condition is null ? old.Previous
                        : [.. old.Previous, (old.Condition, false)], (directive as ElifDirectiveTriviaSyntax)?.Condition));
            }
        }
    }

    private static (ExpressionSyntax, bool)[] WithBranch(OpenBranch branch) => branch.Condition is null
        ? branch.Previous : [.. branch.Previous, (branch.Condition, true)];

    private static bool? FindBranchSymbols((ExpressionSyntax Expression, bool Expected)[] conditions,
                                           Dictionary<string, bool> assignments, ref int budget)
    {
        if (budget-- <= 0) return null;
        ExpressionSyntax? unknown = null;
        foreach (var (expression, expected) in conditions)
        {
            var value = EvaluateCondition(expression, assignments);
            if (value == !expected) return false;
            if (value is null) unknown ??= expression;
        }
        if (unknown is null) return true;
        if (assignments.Count >= 16) return null;
        var symbol = unknown.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>()
            .Select(name => name.Identifier.ValueText).FirstOrDefault(name => !assignments.ContainsKey(name));
        if (symbol is null) return null;
        assignments[symbol] = false;
        var absent = FindBranchSymbols(conditions, assignments, ref budget);
        if (absent == true) return true;
        assignments[symbol] = true;
        var present = FindBranchSymbols(conditions, assignments, ref budget);
        if (present == true) return true;
        assignments.Remove(symbol);
        return absent == false && present == false ? false : null;
    }

    private static bool? EvaluateCondition(ExpressionSyntax expression, Dictionary<string, bool> assignments) =>
        expression switch
        {
            IdentifierNameSyntax name => assignments.TryGetValue(name.Identifier.ValueText, out var value) ? value : null,
            LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.TrueLiteralExpression) => true,
            LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.FalseLiteralExpression) => false,
            ParenthesizedExpressionSyntax group => EvaluateCondition(group.Expression, assignments),
            PrefixUnaryExpressionSyntax not when not.IsKind(SyntaxKind.LogicalNotExpression)
                => !EvaluateCondition(not.Operand, assignments),
            BinaryExpressionSyntax binary => EvaluateBinary(binary.Kind(), EvaluateCondition(binary.Left, assignments),
                EvaluateCondition(binary.Right, assignments)),
            _ => null,
        };

    private static bool? EvaluateBinary(SyntaxKind kind, bool? left, bool? right) => kind switch
    {
        SyntaxKind.LogicalAndExpression => left & right,
        SyntaxKind.LogicalOrExpression => left | right,
        SyntaxKind.EqualsExpression when left.HasValue && right.HasValue => left == right,
        SyntaxKind.NotEqualsExpression when left.HasValue && right.HasValue => left != right,
        _ => null,
    };
}

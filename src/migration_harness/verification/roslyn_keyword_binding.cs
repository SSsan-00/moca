using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

internal static partial class Program
{
    private static (string Binding, string? Retainer) KeywordBinding(SyntaxToken token, SyntaxKind kind,
        Dictionary<SyntaxNode, (string? Binding, string? Retainer)> cache)
    {
        var path = new List<string> { kind.ToString() };
        var blockRetainers = new List<(int Index, string Part)>();
        foreach (var node in token.Parent!.AncestorsAndSelf().Reverse())
        {
            if (node is BaseNamespaceDeclarationSyntax space)
            {
                path.AddRange(space.Name.DescendantTokens().Where(t => t.IsKind(SyntaxKind.IdentifierToken))
                    .Select(t => "namespace:" + t.ValueText));
                continue;
            }
            if (node is VariableDeclarationSyntax variable && variable.Type.Span.Contains(token.Span))
                path.Add("declared-type:" + JsonSerializer.Serialize(variable.Variables.Select(v => v.Identifier.ValueText)));
            if (!cache.TryGetValue(node, out var part))
            {
                if (node is AnonymousFunctionExpressionSyntax callback)
                {
                    IndexKeywordCallbacks(callback, cache);
                    part = cache[node];
                }
                else cache[node] = part = KeywordScopePart(node);
            }
            if (part.Binding is null) continue;
            if (part.Retainer is not null) blockRetainers.Add((path.Count, part.Retainer));
            path.Add(part.Binding);
        }
        var binding = JsonSerializer.Serialize(path);
        if (blockRetainers.Count == 0) return (binding, IsNamedKeywordMember(token) ? binding : null);
        foreach (var (index, part) in blockRetainers) path[index] = part;
        return (binding, JsonSerializer.Serialize(path));
    }

    private static (string? Binding, string? Retainer) KeywordScopePart(SyntaxNode node)
    {
        if (KeywordNamedScope(node) is { } named) return (named, null);
        if (KeywordUnnamedScope(node) is not { } shape) return (null, null);
        var siblings = node.Parent?.ChildNodes().Where(sibling =>
            sibling.RawKind == node.RawKind && KeywordUnnamedScope(sibling) == shape);
        var ordinal = siblings?.TakeWhile(sibling => sibling.SpanStart < node.SpanStart).Count() ?? 0;
        if (node.Parent is not (BlockSyntax or SwitchSectionSyntax or SwitchStatementSyntax or SwitchExpressionSyntax
            or TryStatementSyntax)) return (ordinal + ":" + shape, null);
        // An inserted or deleted sibling makes an anonymous scope's ordinal insufficient on its own.
        var content = SHA256.HashData(Encoding.UTF8.GetBytes(KeywordHeader(node)));
        return ($"{ordinal}/{siblings?.Count() ?? 1}:{shape}", $"{ordinal}:scope-content:" + Convert.ToHexString(content));
    }

    private static bool IsCallbackOwner(SyntaxNode node) => node is BlockSyntax or SwitchSectionSyntax
        or AnonymousFunctionExpressionSyntax or VariableDeclaratorSyntax or LocalFunctionStatementSyntax
        or CompilationUnitSyntax or MemberDeclarationSyntax and not GlobalStatementSyntax;

    private static void IndexKeywordCallbacks(AnonymousFunctionExpressionSyntax callback,
        Dictionary<SyntaxNode, (string? Binding, string? Retainer)> cache)
    {
        var owner = callback.Ancestors().First(IsCallbackOwner);
        var callbacks = owner.DescendantNodes(node => node == owner || !IsCallbackOwner(node))
            .OfType<AnonymousFunctionExpressionSyntax>();
        foreach (var group in callbacks.GroupBy(node => CallbackSite(node, owner)))
        {
            // Repeated indistinguishable calls must keep both their order and their count.
            var ordinal = 0;
            foreach (var node in group)
                cache[node] = ($"callback:{ordinal++}/{group.Count()}:{group.Key}", null);
        }
    }

    private static string CallbackSite(AnonymousFunctionExpressionSyntax callback, SyntaxNode owner)
    {
        var path = new List<string> { callback.Kind().ToString() };
        for (SyntaxNode child = callback; child.Parent is { } parent && parent != owner; child = parent)
        {
            if (parent is ParenthesizedExpressionSyntax) continue;
            var slot = (parent, child) switch
            {
                (BaseArgumentListSyntax list, ArgumentSyntax arg) => arg.NameColon is { } name
                    ? "named:" + name.Name.Identifier.ValueText : "position:" + list.Arguments.IndexOf(arg),
                (InvocationExpressionSyntax call, ArgumentListSyntax) => KeywordHeader(call.Expression),
                (ObjectCreationExpressionSyntax creation, ArgumentListSyntax) => KeywordHeader(creation.Type),
                (AssignmentExpressionSyntax assignment, _) when child == assignment.Right => KeywordHeader(assignment.Left),
                _ => parent.ChildNodes().TakeWhile(sibling => sibling != child).Count().ToString(),
            };
            path.Add(parent.RawKind + ":" + slot);
        }
        return JsonSerializer.Serialize(path);
    }

    private static bool IsNamedKeywordMember(SyntaxToken token) => token.Parent switch
    {
        VariableDeclaratorSyntax { Parent.Parent: FieldDeclarationSyntax or EventFieldDeclarationSyntax } => true,
        BaseTypeDeclarationSyntax or DelegateDeclarationSyntax or MethodDeclarationSyntax or PropertyDeclarationSyntax
            or EventDeclarationSyntax or EnumMemberDeclarationSyntax => true,
        _ => false,
    };

    private static string? KeywordNamedScope(SyntaxNode node) => node switch
    {
        TypeDeclarationSyntax type when !type.Identifier.IsMissing => $"type:{type.Identifier.ValueText}:{type.Arity}",
        EnumDeclarationSyntax type => "enum:" + type.Identifier.ValueText,
        DelegateDeclarationSyntax member => MemberScope("delegate:" + member.Identifier.ValueText, member.TypeParameterList,
            member.ParameterList),
        MethodDeclarationSyntax member => MemberScope("method:" + member.Identifier.ValueText, member.TypeParameterList,
            member.ParameterList, member.ExplicitInterfaceSpecifier),
        LocalFunctionStatementSyntax member => MemberScope("local:" + member.Identifier.ValueText, member.TypeParameterList,
            member.ParameterList),
        ConstructorDeclarationSyntax member => MemberScope(".ctor", null, member.ParameterList),
        DestructorDeclarationSyntax => ".dtor",
        OperatorDeclarationSyntax member => MemberScope("operator:" + member.OperatorToken.ValueText, null,
            member.ParameterList),
        ConversionOperatorDeclarationSyntax member => MemberScope("convert:" + KeywordHeader(member.Type), null,
            member.ParameterList),
        PropertyDeclarationSyntax member => "property:" + (member.ExplicitInterfaceSpecifier is { } contract
            ? KeywordHeader(contract) : "") + member.Identifier.ValueText,
        EventDeclarationSyntax member => "event:" + (member.ExplicitInterfaceSpecifier is { } contract
            ? KeywordHeader(contract) : "") + member.Identifier.ValueText,
        IndexerDeclarationSyntax member => MemberScope("indexer", null, member.ParameterList,
            member.ExplicitInterfaceSpecifier),
        AccessorDeclarationSyntax accessor => "accessor:" + accessor.Keyword.ValueText,
        VariableDeclaratorSyntax variable => "variable:" + variable.Identifier.ValueText,
        _ => null,
    };

    private static string MemberScope(string name, TypeParameterListSyntax? typeParameters, BaseParameterListSyntax parameters,
                                      ExplicitInterfaceSpecifierSyntax? contract = null) =>
        JsonSerializer.Serialize(new[] { name, (typeParameters?.Parameters.Count ?? 0).ToString(),
            contract is null ? "" : KeywordHeader(contract) }.Concat(parameters.Parameters.Select(parameter =>
                KeywordTokens(parameter.Modifiers.Where(modifier => modifier.Kind() is
                        SyntaxKind.RefKeyword or SyntaxKind.InKeyword or SyntaxKind.OutKeyword)
                    .Concat(parameter.Type?.DescendantTokens() ?? [])))));

    private static string? KeywordUnnamedScope(SyntaxNode node) => node switch
    {
        BlockSyntax { Parent: BlockSyntax or SwitchSectionSyntax or GlobalStatementSyntax } => "block",
        BlockSyntax => null,
        TypeDeclarationSyntax => "incomplete-type:" + node.RawKind,
        SwitchStatementSyntax statement => "switch:" + KeywordHeader(statement.Expression),
        SwitchExpressionSyntax expression => "switch-expression:" + KeywordHeader(expression.GoverningExpression),
        IfStatementSyntax branch => "if:" + KeywordHeader(branch.Condition),
        ElseClauseSyntax => "else",
        SwitchSectionSyntax section => "section:" + KeywordTokens(section.Labels.SelectMany(label => label.DescendantTokens())),
        CatchClauseSyntax handler => "catch:" + KeywordTokens((handler.Declaration?.Type.DescendantTokens() ?? [])
            .Concat(handler.Filter?.DescendantTokens() ?? [])),
        FinallyClauseSyntax => "finally",
        SwitchExpressionArmSyntax arm => "arm:" + KeywordTokens(arm.Pattern.DescendantTokens()
            .Concat(arm.WhenClause?.DescendantTokens() ?? [])),
        QueryExpressionSyntax => "query",
        StatementSyntax statement when statement.ChildNodes().OfType<StatementSyntax>().FirstOrDefault() is { } body
            => node.RawKind + ":" + KeywordTokens(statement.DescendantTokens().TakeWhile(token => token.SpanStart < body.SpanStart)),
        _ => null,
    };

    private static string KeywordHeader(SyntaxNode node) => KeywordTokens(node.DescendantTokens());

    private static string KeywordTokens(IEnumerable<SyntaxToken> tokens) => JsonSerializer.Serialize(tokens.Select(token =>
        token.IsKind(SyntaxKind.IdentifierToken) ? token.ValueText : token.Text));
}

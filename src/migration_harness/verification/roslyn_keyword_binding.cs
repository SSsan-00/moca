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
        var sites = owner.DescendantNodes(node => node == owner || !IsCallbackOwner(node))
            .Where(node => node is AnonymousFunctionExpressionSyntax or BaseArgumentListSyntax).ToArray();
        var callbacks = sites.OfType<AnonymousFunctionExpressionSyntax>().ToArray();
        var needed = callbacks.SelectMany(cb => cb.Ancestors().TakeWhile(node => node != owner))
            .OfType<ArgumentSyntax>().ToHashSet();
        var positions = new Dictionary<ArgumentSyntax, string>();
        foreach (var group in sites.OfType<BaseArgumentListSyntax>().GroupBy(list => CallbackSite(list, owner)))
        {
            var arguments = group.SelectMany(list => list.Arguments).ToLookup(ArgumentRole);
            foreach (var role in arguments.Where(role => role.Any(needed.Contains)))
            {
                // Include calls whose slot contains a method group, not just calls already containing a lambda.
                foreach (var peers in role.GroupBy(OtherArguments))
                {
                    var ordinal = 0;
                    foreach (var arg in peers) positions[arg] = $"{ordinal++}/{peers.Count()}:{peers.Key}";
                }
            }
        }
        foreach (var group in callbacks.GroupBy(node => CallbackSite(node, owner, positions)))
        {
            var ordinal = 0;
            foreach (var node in group) cache[node] = ($"callback:{ordinal++}/{group.Count()}:{group.Key}", null);
        }
    }

    private static string CallbackSite(SyntaxNode node, SyntaxNode owner,
        IReadOnlyDictionary<ArgumentSyntax, string>? positions = null)
    {
        var path = new List<string> { node.Kind().ToString() };
        for (var child = node; child.Parent is { } parent && parent != owner; child = parent)
        {
            if (parent is ParenthesizedExpressionSyntax) continue;
            var slot = (parent, child) switch
            {
                (BaseArgumentListSyntax, ArgumentSyntax arg) => ArgumentRole(arg),
                (InvocationExpressionSyntax call, ArgumentListSyntax) => CallbackHeader(call.Expression),
                (ConditionalAccessExpressionSyntax access, _) when child == access.WhenNotNull => CallbackHeader(access.Expression),
                (ObjectCreationExpressionSyntax creation, ArgumentListSyntax) => KeywordHeader(creation.Type),
                (AssignmentExpressionSyntax assignment, _) when child == assignment.Right => CallbackHeader(assignment.Left),
                _ => parent.ChildNodes().TakeWhile(sibling => sibling != child).Count().ToString(),
            };
            if (positions is not null && child is ArgumentSyntax argSlot && parent is BaseArgumentListSyntax)
                slot += ":" + positions[argSlot];
            path.Add(parent.RawKind + ":" + slot);
        }
        return JsonSerializer.Serialize(path);
    }

    private static string ArgumentRole(ArgumentSyntax arg) => arg.NameColon is { } name
        ? "name:" + name.Name.Identifier.ValueText : "slot:" + ((BaseArgumentListSyntax)arg.Parent!).Arguments.IndexOf(arg);

    private static string OtherArguments(ArgumentSyntax current) => JsonSerializer.Serialize(
        ((BaseArgumentListSyntax)current.Parent!).Arguments.Where(arg => arg != current)
            .Select(arg => ArgumentRole(arg) + ":" + CallbackHeader(arg.Expression)).Order(StringComparer.Ordinal));

    private static string CallbackHeader(SyntaxNode node) =>
        KeywordTokens(node.DescendantTokens(child => child is not AnonymousFunctionExpressionSyntax));

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

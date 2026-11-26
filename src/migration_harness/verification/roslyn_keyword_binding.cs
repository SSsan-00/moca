using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

internal static partial class Program
{
    private static string KeywordBinding(SyntaxToken token, SyntaxKind kind, Dictionary<SyntaxNode, string?> cache)
    {
        var path = new List<string> { kind.ToString() };
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
                part = KeywordNamedScope(node);
                if (part is null && KeywordUnnamedScope(node) is { } shape)
                {
                    var ordinal = node.Parent?.ChildNodes().TakeWhile(sibling => sibling.SpanStart < node.SpanStart)
                        .Count(sibling => sibling.RawKind == node.RawKind && KeywordUnnamedScope(sibling) == shape) ?? 0;
                    part = ordinal + ":" + shape;
                }
                cache.Add(node, part);
            }
            if (part is not null) path.Add(part);
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
        AnonymousFunctionExpressionSyntax => "anonymous:" + node.RawKind,
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

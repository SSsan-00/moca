using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

const int MaxRequestCharacters = 8_000_000;

Console.InputEncoding = new UTF8Encoding(false, true);
Console.OutputEncoding = new UTF8Encoding(false);
object result;
try
{
    var request = new StringBuilder();
    var buffer = new char[4096];
    while (Console.In.Read(buffer, 0, buffer.Length) is var count && count > 0)
    {
        if (request.Length + count > MaxRequestCharacters)
            throw new Unverified("source_limit");
        request.Append(buffer, 0, count);
    }
    using var json = JsonDocument.Parse(request.ToString());
    result = Extract(json.RootElement);
}
catch (Unverified e) { result = Unavailable(e.Message); }
catch (Exception e) when (e is JsonException or DecoderFallbackException)
{
    result = Unavailable("invalid_request");
}
try
{
    var json = JsonSerializer.Serialize(result, new JsonSerializerOptions { MaxDepth = 128 });
    Console.WriteLine(json.Length <= 1_000_000 ? json : JsonSerializer.Serialize(Unavailable("shape_limit")));
}
catch (JsonException) { Console.WriteLine(JsonSerializer.Serialize(Unavailable("shape_limit"))); }

static object Unavailable(string reason) => new
{
    status = "unverified", reason, @params = Array.Empty<string>(), return_type = (string?)null,
    body = Array.Empty<object>()
};

static object Extract(JsonElement request)
{
    if (request.ValueKind != JsonValueKind.Object)
        throw new Unverified("invalid_request");
    var path = RequiredString(request, "path");
    var text = RequiredString(request, "text");
    var name = RequiredString(request, "method");
    var first = RequiredLine(request, "start_line");
    var last = RequiredLine(request, "end_line");
    if (path.Length == 0 || path.Contains('\0') || !path.EndsWith(".cs", StringComparison.OrdinalIgnoreCase)
        || string.IsNullOrWhiteSpace(text) || string.IsNullOrWhiteSpace(name) || last < first)
        throw new Unverified("invalid_request");
    if (text.Length > 1_000_000)
        throw new Unverified("source_limit");

    var source = SourceText.From(text);
    var tree = CSharpSyntaxTree.ParseText(source, new CSharpParseOptions(LanguageVersion.CSharp13), path);
    var root = tree.GetRoot();
    if (root.DescendantTrivia(descendIntoTrivia: true).Any(t =>
        t.IsKind(SyntaxKind.IfDirectiveTrivia) || t.IsKind(SyntaxKind.ErrorDirectiveTrivia)))
        throw new Unverified("project_preprocessor_required");
    if (tree.GetDiagnostics().Any(d => d.Severity == DiagnosticSeverity.Error))
        throw new Unverified("syntax_error");

    int Line(int position) => source.Lines.GetLineFromPosition(position).LineNumber + 1;
    var named = root.DescendantNodes().OfType<MethodDeclarationSyntax>().Where(m => m.Identifier.ValueText == name).ToArray();
    if (named.Length == 0)
        throw new Unverified("method_not_found");
    var exact = named.Where(m => Line(m.SpanStart) == first && Line(m.Span.End - 1) == last).ToArray();
    if (exact.Length != 1)
        throw new Unverified(exact.Length == 0 ? "range_mismatch" : "ambiguous_method");
    var method = exact[0];
    if (method.TypeParameterList is not null || method.ExplicitInterfaceSpecifier is not null
        || method.Modifiers.Any(token => !AllowedMethodModifier(token.Kind()))
        || method.AttributeLists.Count != 0 || (method.Body is null && method.ExpressionBody is null))
        throw new Unverified("unsupported_signature");

    var returnType = ScalarType(method.ReturnType);
    var parameters = method.ParameterList.Parameters.ToArray();
    if (parameters.Length > 16)
        throw new Unverified("shape_limit");
    var parameterTypes = parameters.Select(p =>
        p.Modifiers.Count == 0 && p.Default is null && p.AttributeLists.Count == 0
            ? ScalarType(p.Type) : throw new Unverified("unsupported_signature")).ToArray();
    if (parameters.Select(p => p.Identifier.ValueText).Distinct(StringComparer.Ordinal).Count() != parameters.Length)
        throw new Unverified("semantic_error");
    var positions = parameters.Select((p, i) => (p.Identifier.ValueText, i))
        .ToDictionary(pair => pair.ValueText, pair => pair.i, StringComparer.Ordinal);

    var compilation = CSharpCompilation.Create("ControlFlowEvidence", [tree],
        [MetadataReference.CreateFromFile(typeof(object).Assembly.Location)],
        new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
    var model = compilation.GetSemanticModel(tree);
    if (model.GetDeclaredSymbol(method) is not IMethodSymbol symbol
        || model.GetDiagnostics(method.Span).Any(d => d.Severity == DiagnosticSeverity.Error))
        throw new Unverified("semantic_error");

    var encoder = new Encoder(model, symbol, positions, parameterTypes, returnType, Line);
    var body = method.ExpressionBody is { } expression
        ? new[] { encoder.Return(expression.Expression, Line(expression.SpanStart), 0) }
        : encoder.Statements(method.Body!.Statements, 0);
    return new { status = "supported", reason = "ok", @params = parameterTypes, return_type = returnType, body };
}

static string RequiredString(JsonElement request, string key) =>
    request.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
        ? value.GetString()! : throw new Unverified("invalid_request");

static int RequiredLine(JsonElement request, string key) =>
    request.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.Number
        && value.TryGetInt32(out var number) && number > 0
        ? number : throw new Unverified("invalid_request");

static bool AllowedMethodModifier(SyntaxKind kind) => kind is SyntaxKind.PublicKeyword or SyntaxKind.PrivateKeyword
    or SyntaxKind.ProtectedKeyword or SyntaxKind.InternalKeyword or SyntaxKind.StaticKeyword;

static string ScalarType(TypeSyntax? type) => type is PredefinedTypeSyntax predefined ? predefined.Keyword.Kind() switch
{
    SyntaxKind.IntKeyword => "int",
    SyntaxKind.BoolKeyword => "bool",
    _ => throw new Unverified("unsupported_signature")
} : throw new Unverified("unsupported_signature");

internal sealed class Unverified(string reason) : Exception(reason);

internal sealed class Encoder(SemanticModel model, IMethodSymbol method, Dictionary<string, int> positions,
    string[] parameterTypes, string returnType, Func<int, int> line)
{
    private int _nodes;

    private void Count(int depth)
    {
        if (++_nodes > 1_000 || depth > 50)
            throw new Unverified("shape_limit");
    }

    public object[] Statements(SyntaxList<StatementSyntax> statements, int depth) =>
        statements.SelectMany(statement => Statement(statement, depth)).ToArray();

    public object Return(ExpressionSyntax expression, int location, int depth)
    {
        var value = Expression(expression, depth + 1);
        if (value.Type != returnType)
            throw new Unverified("unsupported_expression");
        return new { op = "return", line = location, value = value.Ir };
    }

    private object[] Statement(StatementSyntax statement, int depth)
    {
        Count(depth);
        var location = line(statement.SpanStart);
        switch (statement)
        {
            case BlockSyntax block:
                return Statements(block.Statements, depth + 1);
            case ReturnStatementSyntax { Expression: { } expression }:
                return [Return(expression, location, depth)];
            case IfStatementSyntax conditional:
            {
                var condition = Expression(conditional.Condition, depth + 1);
                if (condition.Type != "bool")
                    throw new Unverified("unsupported_expression");
                return [Conditional(location, condition.Ir, Statement(conditional.Statement, depth + 1),
                    conditional.Else is { } other ? Statement(other.Statement, depth + 1) : [])];
            }
            case SwitchStatementSyntax selection:
                return Switch(selection, depth);
            default:
                throw new Unverified("unsupported_statement");
        }
    }

    private static object Conditional(int location, object test, object[] then, object[] otherwise) =>
        new { op = "if", line = location, test, then, @else = otherwise };

    private object[] Switch(SwitchStatementSyntax selection, int depth)
    {
        var input = Expression(selection.Expression, depth + 1);
        var branches = new List<(object Test, object[] Then, int Line)>();
        object[]? other = null;
        if (selection.Sections.Count > 24)
            throw new Unverified("shape_limit");
        foreach (var section in selection.Sections)
        {
            if (section.Statements.Count == 0 || !AlwaysReturns(section.Statements.Last(), depth + 1))
                throw new Unverified("unsupported_switch");
            var body = Statements(section.Statements, depth + 1);
            if (section.Labels.Count == 1 && section.Labels[0] is DefaultSwitchLabelSyntax)
            {
                if (other is not null)
                    throw new Unverified("unsupported_switch");
                other = body;
                continue;
            }
            object? condition = null;
            if (section.Labels.Count > 24)
                throw new Unverified("shape_limit");
            foreach (var label in section.Labels)
            {
                Count(depth + 1);
                var syntax = label switch
                {
                    CaseSwitchLabelSyntax regular => regular.Value,
                    CasePatternSwitchLabelSyntax { Pattern: ConstantPatternSyntax constant, WhenClause: null } => constant.Expression,
                    _ => throw new Unverified("unsupported_switch")
                };
                var part = Expression(syntax, depth + 1);
                if (part.Type != input.Type || part.Ir[0] is not ("bool" or "int"))
                    throw new Unverified("unsupported_switch");
                object[] equal = ["==", input.Ir, part.Ir];
                condition = condition is null ? equal : new object[] { "||", condition, equal };
            }
            branches.Add((condition ?? throw new Unverified("unsupported_switch"), body, line(section.SpanStart)));
        }
        if (other is null)
            throw new Unverified("unsupported_switch");
        if (branches.Count + depth > 50)
            throw new Unverified("shape_limit");
        for (var i = branches.Count - 1; i >= 0; i--)
        {
            var branch = branches[i];
            other = [Conditional(branch.Line, branch.Test, branch.Then, other)];
        }
        return other;
    }

    private static bool AlwaysReturns(StatementSyntax statement, int depth)
    {
        if (depth > 50)
            throw new Unverified("shape_limit");
        return statement switch
        {
            ReturnStatementSyntax => true,
            BlockSyntax block when block.Statements.Count != 0 => AlwaysReturns(block.Statements.Last(), depth + 1),
            IfStatementSyntax { Else: { } other } conditional =>
                AlwaysReturns(conditional.Statement, depth + 1) && AlwaysReturns(other.Statement, depth + 1),
            _ => false
        };
    }

    private (string Type, object[] Ir) Expression(ExpressionSyntax expression, int depth)
    {
        Count(depth);
        switch (expression)
        {
            case ParenthesizedExpressionSyntax group:
                return Expression(group.Expression, depth + 1);
            case LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.TrueLiteralExpression):
                return ("bool", ["bool", true]);
            case LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.FalseLiteralExpression):
                return ("bool", ["bool", false]);
            case LiteralExpressionSyntax literal when literal.IsKind(SyntaxKind.NumericLiteralExpression):
            case PrefixUnaryExpressionSyntax { Operand: LiteralExpressionSyntax } when
                expression.IsKind(SyntaxKind.UnaryMinusExpression) || expression.IsKind(SyntaxKind.UnaryPlusExpression):
                if (model.GetTypeInfo(expression).Type?.SpecialType == SpecialType.System_Int32
                    && model.GetConstantValue(expression) is { HasValue: true, Value: int value })
                    return ("int", ["int", value]);
                break;
            case IdentifierNameSyntax identifier when positions.TryGetValue(identifier.Identifier.ValueText, out var index):
                if (model.GetSymbolInfo(identifier).Symbol is IParameterSymbol parameter
                    && parameter.Ordinal == index && SymbolEqualityComparer.Default.Equals(parameter.ContainingSymbol, method))
                    return (parameterTypes[index], ["arg", index]);
                break;
            case PrefixUnaryExpressionSyntax unary when unary.IsKind(SyntaxKind.LogicalNotExpression):
                var operand = Expression(unary.Operand, depth + 1);
                if (operand.Type == "bool")
                    return ("bool", ["not", operand.Ir]);
                break;
            case BinaryExpressionSyntax binary:
                return Binary(binary, depth);
        }
        throw new Unverified("unsupported_expression");
    }

    private (string Type, object[] Ir) Binary(BinaryExpressionSyntax binary, int depth)
    {
        var left = Expression(binary.Left, depth + 1);
        var right = Expression(binary.Right, depth + 1);
        if (left.Type != right.Type)
            throw new Unverified("unsupported_expression");
        var op = binary.Kind() switch
        {
            SyntaxKind.LogicalAndExpression when left.Type == "bool" => "&&",
            SyntaxKind.LogicalOrExpression when left.Type == "bool" => "||",
            SyntaxKind.EqualsExpression => "==",
            SyntaxKind.NotEqualsExpression => "!=",
            SyntaxKind.LessThanExpression when left.Type == "int" => "<",
            SyntaxKind.LessThanOrEqualExpression when left.Type == "int" => "<=",
            SyntaxKind.GreaterThanExpression when left.Type == "int" => ">",
            SyntaxKind.GreaterThanOrEqualExpression when left.Type == "int" => ">=",
            _ => throw new Unverified("unsupported_expression")
        };
        return ("bool", [op, left.Ir, right.Ir]);
    }
}

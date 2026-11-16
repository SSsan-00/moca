// Parse source only: never load the target assembly or invoke business code.
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using System.Text.Json;

var trees = args.Select(path => CSharpSyntaxTree.ParseText(File.ReadAllText(path), path: path))
    .Append(CSharpSyntaxTree.ParseText("global using System; global using System.Collections.Generic; global using System.Linq;"))
    .ToArray();
var references = ((string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!).Split(Path.PathSeparator)
    .Select(path => MetadataReference.CreateFromFile(path));
var compilation = CSharpCompilation.Create("Inspection", trees, references);
var policies = new List<Dictionary<string, object?>>();
var strings = new HashSet<string>();
var problems = new List<string>();
bool transactionBinding = false, nullBinding = false, parameterBinding = false;
foreach (var tree in trees.Where(tree => Path.GetFileName(tree.FilePath) != "Contracts.cs"))
{
    var model = compilation.GetSemanticModel(tree);
    foreach (var node in tree.GetRoot().DescendantNodes())
    {
        if (node is ExpressionSyntax expression && model.GetConstantValue(expression) is { HasValue: true, Value: string text })
            strings.Add(text);
        if (node is BaseObjectCreationExpressionSyntax creation &&
            model.GetSymbolInfo(creation).Symbol is IMethodSymbol constructor &&
            constructor.ContainingType.Name == "PolicySpec")
        {
            var row = new Dictionary<string, object?>();
            var arguments = creation.ArgumentList!.Arguments;
            for (var i = 0; i < arguments.Count; i++)
            {
                var argument = arguments[i];
                var name = argument.NameColon?.Name.Identifier.ValueText ?? constructor.Parameters[i].Name;
                var constant = model.GetConstantValue(argument.Expression);
                if (constant.HasValue) row[name] = constant.Value;
                else problems.Add($"PolicySpec.{name} must be a statically inspectable constant.");
            }
            policies.Add(row);
        }
        if (node is CastExpressionSyntax cast && cast.Type.ToString() == "int" &&
            model.GetTypeInfo(cast.Expression).Type?.SpecialType == SpecialType.System_Object)
            problems.Add("Direct (int) unboxing of object can fail for SQLite Int64; convert explicitly.");
        if (node is InvocationExpressionSyntax call && call.Expression is MemberAccessExpressionSyntax member)
        {
            parameterBinding |= member.Name.Identifier.ValueText == "AddWithValue";
            if (member.Name.Identifier.ValueText == "BeginTransaction" && call.Parent is ExpressionStatementSyntax)
                problems.Add("The BeginTransaction return value is discarded.");
        }
        if (node is AssignmentExpressionSyntax assignment && assignment.Left is MemberAccessExpressionSyntax left)
            transactionBinding |= left.Name.Identifier.ValueText == "Transaction";
        if (node is ObjectCreationExpressionSyntax command && command.Type.ToString().EndsWith("SqliteCommand"))
            transactionBinding |= command.ArgumentList?.Arguments.Count >= 3;
        if (node is MemberAccessExpressionSyntax access)
            nullBinding |= access.ToString() is "DBNull.Value" or "System.DBNull.Value";
    }
}
Console.WriteLine(JsonSerializer.Serialize(new { policies, strings, problems, transactionBinding, nullBinding, parameterBinding }));

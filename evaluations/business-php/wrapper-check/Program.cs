using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Operations;

const string RuleId = "WRAP001";
const string WrapperMetadataName = "Synthetic.Legacy.PhpDb";
const string MethodName = "Execute";

Console.OutputEncoding = System.Text.Encoding.UTF8;

if (args.Length < 2)
{
    Console.WriteLine($"{RuleId} unknown <inputs>:1:1: pass the trusted contract source followed by target C# source files.");
    return 2;
}

try
{
    var contract = Parse(args[0]);
    var targets = args[1..].Select(Parse).ToArray();
    if (targets.Any(tree => string.Equals(tree.FilePath, contract.FilePath, StringComparison.OrdinalIgnoreCase)))
    {
        Console.WriteLine($"{RuleId} unknown {contract.FilePath}:1:1: contract and target inputs must be separate.");
        return 2;
    }

    var platform = (string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!;
    var references = platform.Split(Path.PathSeparator).Select(path => MetadataReference.CreateFromFile(path));
    var compilation = CSharpCompilation.Create("SyntheticWrapperInspection", targets.Prepend(contract), references,
        new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
    var expectedType = compilation.GetTypeByMetadataName(WrapperMetadataName);
    var expected = expectedType?.GetMembers(MethodName).OfType<IMethodSymbol>().SingleOrDefault(method =>
        method.IsStatic && method.Arity == 0 &&
        method.Parameters is [{ RefKind: RefKind.None, Type.SpecialType: SpecialType.System_Int64 }] &&
        method.Locations.Any(location => location.SourceTree == contract));
    if (expected is null)
    {
        Console.WriteLine($"{RuleId} unknown {contract.FilePath}:1:1: cannot establish the trusted PhpDb.Execute(long) contract.");
        return 2;
    }

    var errors = compilation.GetDiagnostics().Where(diagnostic => diagnostic.Severity == DiagnosticSeverity.Error).ToArray();
    var result = 0;
    var checkedCalls = 0;
    foreach (var tree in targets)
    {
        var semantic = compilation.GetSemanticModel(tree);
        foreach (var call in tree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>().Where(IsCandidate))
        {
            checkedCalls++;
            if (semantic.GetOperation(call) is not IInvocationOperation bound ||
                bound.TargetMethod.ContainingType.TypeKind == TypeKind.Error)
            {
                Report(tree, call.GetLocation(), "unknown", "cannot resolve the invocation to a method symbol");
                result = 2;
                continue;
            }
            if (!SymbolEqualityComparer.Default.Equals(bound.TargetMethod.OriginalDefinition, expected))
            {
                Report(tree, call.GetLocation(), "violation",
                    $"expected {WrapperMetadataName}.{MethodName}(long), found {bound.TargetMethod.ToDisplayString()}");
                result = Math.Max(1, result);
                continue;
            }
            var expression = call.ArgumentList.Arguments.SingleOrDefault()?.Expression;
            var originalType = expression is null ? null : semantic.GetTypeInfo(expression).Type;
            if (originalType is null || originalType.TypeKind is TypeKind.Error or TypeKind.Dynamic)
            {
                Report(tree, call.GetLocation(), "unknown", "cannot establish the argument type before conversion");
                result = 2;
            }
            else if (originalType.SpecialType != SpecialType.System_Int64)
            {
                Report(tree, expression!.GetLocation(), "violation",
                    $"expected argument type long before conversion, found {originalType.ToDisplayString()}");
                result = Math.Max(1, result);
            }
        }
    }

    foreach (var error in errors)
    {
        Report(error.Location.SourceTree ?? targets[0], error.Location, "unknown",
            $"compiler could not check all source ({error.Id}): {error.GetMessage()}");
        result = 2;
    }

    if (checkedCalls == 0 && result == 0)
    {
        Report(targets[0], Location.None, "violation", "required Execute invocation is absent from the supplied sources");
        result = 1;
    }
    if (result == 0)
        Console.WriteLine("特に気になる点はありません。");
    return result;
}
catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException)
{
    Console.WriteLine($"{RuleId} unknown <inputs>:1:1: {error.Message}");
    return 2;
}

static SyntaxTree Parse(string path)
{
    var fullPath = Path.GetFullPath(path);
    return CSharpSyntaxTree.ParseText(File.ReadAllText(fullPath), path: fullPath);
}

static bool IsCandidate(InvocationExpressionSyntax call)
{
    SimpleNameSyntax? name = call.Expression switch
    {
        SimpleNameSyntax simple => simple,
        MemberAccessExpressionSyntax member => member.Name,
        MemberBindingExpressionSyntax member => member.Name,
        _ => null,
    };
    return name?.Identifier.ValueText == MethodName;
}

static void Report(SyntaxTree tree, Location location, string kind, string reason)
{
    var position = location.IsInSource ? location.GetLineSpan().StartLinePosition : default;
    var positionText = $"{tree.FilePath}:{position.Line + 1}:{position.Character + 1}";
    Console.WriteLine(kind == "unknown"
        ? $"{RuleId} unknown {positionText}: {reason}"
        : $"{RuleId} {positionText} error: {reason}");
}

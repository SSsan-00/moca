namespace Unrelated;

internal static class PhpDb
{
    internal static void Execute(long amount) { }
}

internal static class WrongSymbol
{
    internal static void Write(long amount) => PhpDb.Execute(amount);
}

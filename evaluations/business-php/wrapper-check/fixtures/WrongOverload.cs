using Synthetic.Legacy;

internal static class WrongOverload
{
    internal static void Write(long amount) => PhpDb.Execute(amount, audit: true);
}

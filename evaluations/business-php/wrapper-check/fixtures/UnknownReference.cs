using Synthetic.Legacy;

internal static class UnknownReference
{
    internal static void Write(MissingAmount amount) => PhpDb.Execute(amount);
}

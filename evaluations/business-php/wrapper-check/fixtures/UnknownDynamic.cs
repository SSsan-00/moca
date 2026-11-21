using Synthetic.Legacy;

internal static class UnknownDynamic
{
    internal static void Write(dynamic amount) => PhpDb.Execute(amount);
}

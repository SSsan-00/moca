using Synthetic.Legacy;

internal static class WrongArgument
{
    internal static void Write(int amount) => PhpDb.Execute(amount);
}

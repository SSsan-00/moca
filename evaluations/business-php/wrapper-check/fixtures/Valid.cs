using Wrapper = Synthetic.Legacy.PhpDb;

internal static class Valid
{
    internal static void Write(long amount)
    {
        Wrapper.Execute(amount);
        global::Synthetic.Legacy.PhpDb.Execute(amount: 14L);
    }
}

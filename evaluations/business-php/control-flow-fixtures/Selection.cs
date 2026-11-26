public static class Selection
{
    public static int Boundary(int quantity) {
        if (quantity > 3) {
            return 1;
        }
        return 0;
    }
    public static int Preferred(int total, bool preferred) {
        if (total >= 3 && preferred) {
            return 7;
        }
        return 9;
    }
    public static bool GuardInverted(int total) {
        if (total >= 0) {
            return true;
        } else {
            return false;
        }
    }
    public static int OmittedBranch(int tier) {
        if (tier >= 10) {
            return 1;
        }
        return 3;
    }
    public static bool Distinct(int one, int two) {
        return one != two;
    }
    public static int WrongOrder(int limit) {
        if (limit >= 5) {
            return 2;
        }
        if (limit >= 10) {
            return 1;
        }
        return 3;
    }
    public static bool TestTruthiness(int kind) {
        return kind != 0;
    }
    public static bool IsFirst(int number, bool enabled) {
        return number == 1 && enabled;
    }
    public static bool PureExpression(bool enabled) {
        return enabled;
    }
    public static bool CanRepeat(int repeats) {
        return repeats > 0;
    }
    public static bool CallOnDemand(int total) {
        return total >= 0 && ExternalPolicy();
    }
    private static bool ExternalPolicy() => true;
    public static int PickCategory(int converted) {
        switch (converted) {
            case 1:
            case 2:
                return 5;
            default:
                return 9;
        }
    }
}

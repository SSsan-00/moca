namespace Migration;

// Public input contract; the validation never invokes business code. Monetary values are integer minor units.
public sealed record Request(
    string Tenant, string Customer, string Sku, string OrderId,
    string Operation, int Policy, int Quantity, string Channel,
    string? Coupon, string Delivery, string Payment, string Today,
    string? Note, string FailAt, long Amount);

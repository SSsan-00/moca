using System;
using System.Linq;
using System.Text.Json;

using var inputs = JsonDocument.Parse(Console.In.ReadToEnd());
Console.WriteLine(JsonSerializer.Serialize(inputs.RootElement.EnumerateArray().Select(Search)));

static object Search(JsonElement input)
{
    var records = new[]
    {
        new { id = 2, code = "02", label = "返金" },
        new { id = 3, code = "A1", label = "入金" },
        new { id = 1, code = "01", label = "振込" },
    };
    var generatedAt = DateTimeOffset.UtcNow.ToString("O");
    if (!input.TryGetProperty("query", out var query) || query.ValueKind != JsonValueKind.String ||
        string.IsNullOrEmpty(query.GetString()))
    {
        return new { status = "query_required", total = 0, rows = Array.Empty<object>(), generated_at = generatedAt };
    }
    if (!input.TryGetProperty("limit", out var limit) || limit.ValueKind != JsonValueKind.Number ||
        !limit.TryGetInt32(out var count) || count < 1 || count > 2)
    {
        return new { status = "invalid_limit", total = 0, rows = Array.Empty<object>(), generated_at = generatedAt };
    }

    var matches = records.Where(row => row.code.StartsWith(query.GetString(), StringComparison.Ordinal))
        .OrderBy(row => row.id)
        .ToArray();
    return new { status = "ok", total = matches.Length, rows = matches.Take(count), generated_at = generatedAt };
}

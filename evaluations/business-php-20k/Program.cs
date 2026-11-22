using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Migration;

Console.OutputEncoding = new UTF8Encoding(false);
var json = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
var cases = JsonSerializer.Deserialize<Scenario[]>(File.ReadAllText(args[0]), json)!;
var schema = File.ReadAllText(args[1]);
(string Table, string Order)[] tables = [
    ("customers", "tenant,id"), ("products", "tenant,sku"), ("memberships", "tenant,customer,program"),
    ("orders", "tenant,id"), ("ledger", "rowid"), ("events", "rowid")
];
var results = new List<object>(cases.Length);
foreach (var scenario in cases)
{
    using var db = new SqliteConnection("Data Source=:memory:");
    db.Open();
    using var sql = db.CreateCommand();
    sql.CommandText = schema;
    sql.ExecuteNonQuery();
    var steps = scenario.Requests.Select(r => BusinessService.Handle(db, r)).ToArray();
    var state = new Dictionary<string, List<Dictionary<string, object?>>>();
    foreach (var (table, order) in tables)
    {
        sql.CommandText = $"SELECT * FROM {table} ORDER BY {order}";
        using var reader = sql.ExecuteReader();
        var rows = new List<Dictionary<string, object?>>();
        while (reader.Read())
        {
            var values = new Dictionary<string, object?>();
            for (var index = 0; index < reader.FieldCount; index++)
                values.Add(reader.GetName(index), reader.IsDBNull(index) ? null : reader.GetValue(index));
            rows.Add(values);
        }
        state.Add(table, rows);
    }
    results.Add(new { id = scenario.Id, steps, state });
}
Console.WriteLine(JsonSerializer.Serialize(results, json));

sealed record Scenario(string Id, Request[] Requests);

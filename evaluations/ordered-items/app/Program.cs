using System.Net;
using System.Text;

var builder = WebApplication.CreateBuilder();
builder.Logging.ClearProviders();
builder.WebHost.ConfigureKestrel(options => options.Listen(IPAddress.Loopback, 0));
builder.Services.AddRazorPages();

await using var app = builder.Build();
app.MapRazorPages();
await app.StartAsync();
try
{
    using var client = new HttpClient(new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false });
    Console.OutputEncoding = Encoding.UTF8;
    Console.Write(await client.GetStringAsync($"{app.Urls.Single()}/OrderedItems"));
}
finally
{
    await app.StopAsync();
}

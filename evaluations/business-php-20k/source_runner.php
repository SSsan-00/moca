<?php
require $argv[1];
$schema = file_get_contents($argv[2]);
$cases = json_decode(file_get_contents($argv[3]), true, 512, JSON_THROW_ON_ERROR);
$out = [];
$tables = ['customers' => 'tenant,id', 'products' => 'tenant,sku',
    'memberships' => 'tenant,customer,program', 'orders' => 'tenant,id',
    'ledger' => 'rowid', 'events' => 'rowid'];
foreach ($cases as $case) {
    $db = new PDO('sqlite::memory:');
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $db->exec($schema);
    $steps = [];
    foreach ($case['requests'] as $request) {
        $steps[] = business($db, $request);
    }
    $state = [];
    foreach ($tables as $table => $order) {
        $state[$table] = $db->query("SELECT * FROM $table ORDER BY $order")->fetchAll(PDO::FETCH_ASSOC);
    }
    $out[] = ['id'=>$case['id'], 'steps'=>$steps, 'state'=>$state];
}
echo json_encode($out, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE), "\n";

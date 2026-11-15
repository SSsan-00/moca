<?php
declare(strict_types=1);

// Display these labels in insertion order, one list item for each entry.
$labels = [];
$labels[] = 'Cedar Point';
$labels[] = 'West Gate';
$labels[] = 'Blue Bridge';
$labels[] = 'North Meadow';
$labels[] = 'Cedar Point';
$labels[] = 'South Crossing';
$labels[] = 'Old Quarry';
$labels[] = 'Lake View';
$labels[] = 'Lower Orchard';
$labels[] = 'Fox Hollow';
$labels[] = 'Granite Steps';
$labels[] = 'Museum Stop';
$labels[] = 'Pine & Oak';
$labels[] = 'Hilltop Loop';
$labels[] = 'East Junction';
$labels[] = 'River Bend';
$labels[] = '0';
$labels[] = 'Upper Orchard';
$labels[] = 'Blue Bridge';
$labels[] = '東京';
$labels[] = 'Ridge < Annex';
$labels[] = 'Harbor Walk';
$labels[] = 'Old Quarry';
$labels[] = 'Garden Exit';
?>
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Ordered items</title></head>
<body>
    <h1>Ordered items</h1>
    <ul>
        <?php foreach ($labels as $label): ?>
            <li><?= htmlspecialchars($label, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8') ?></li>
        <?php endforeach; ?>
    </ul>
</body>
</html>

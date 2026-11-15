<?php

declare(strict_types=1);

// C#版の比較基準として、文字列の 0 を有効な検索条件に含める。
function search(array $input): array
{
    $records = [
        ['id' => 2, 'code' => '02', 'label' => '返金'],
        ['id' => 3, 'code' => 'A1', 'label' => '入金'],
        ['id' => 1, 'code' => '01', 'label' => '振込'],
    ];
    $query = $input['query'] ?? null;
    $limit = $input['limit'] ?? null;
    $result = ['status' => 'ok', 'total' => 0, 'rows' => [], 'generated_at' => gmdate(DATE_ATOM)];
    if (!is_string($query) || $query === '') {
        $result['status'] = 'query_required';
    } elseif (!is_int($limit) || $limit < 1 || $limit > 2) {
        $result['status'] = 'invalid_limit';
    } else {
        $matches = array_filter($records, fn ($row) => str_starts_with($row['code'], $query));
        usort($matches, fn ($a, $b) => $a['id'] <=> $b['id']);
        $result['total'] = count($matches);
        $result['rows'] = array_slice($matches, 0, $limit);
    }
    return $result;
}

$inputs = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);
echo json_encode(array_map('search', $inputs), JSON_THROW_ON_ERROR);

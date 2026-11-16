<?php
function row(PDO $db, string $sql, array $params): ?array {
    $query = $db->prepare($sql);
    $query->execute($params);
    $value = $query->fetch(PDO::FETCH_ASSOC);
    return $value === false ? null : $value;
}
function execute_sql(PDO $db, string $sql, array $params): int {
    $query = $db->prepare($sql);
    $query->execute($params);
    return $query->rowCount();
}
function reject(string $code): array {
    return ['ok' => false, 'code' => $code];
}
function money_ratio(int $value, int $basisPoints, string $rounding): int {
    $numerator = $value * $basisPoints;
    if ($rounding === 'floor') {
        return intdiv($numerator, 10000);
    }
    if ($rounding === 'ceil') {
        return intdiv($numerator + 9999, 10000);
    }
    return intdiv($numerator + 5000, 10000);
}
function order_result(array $order): array {
    return [
        'ok' => true,
        'code' => $order['state'],
        'order' => $order['id'],
        'amount' => (int)$order['amount'],
        'paid' => (int)$order['paid'],
        'quantity' => (int)$order['quantity'],
        'note' => $order['note'],
        'policy' => (int)$order['policy'],
    ];
}
function audit(PDO $db, array $r, string $kind, int $amount): void {
    execute_sql($db, 'INSERT INTO events(tenant, order_id, kind, amount, at) VALUES(:t,:o,:k,:a,:d)',
        [':t'=>$r['tenant'], ':o'=>$r['order_id'], ':k'=>$kind, ':a'=>$amount, ':d'=>$r['today']]);
}
function business(PDO $db, array $r): array {
    $customer = row($db, 'SELECT * FROM customers WHERE tenant=:t AND id=:c',
        [':t'=>$r['tenant'], ':c'=>$r['customer']]);
    if ($customer === null) {
        return reject('customer_missing');
    }
    if ((int)$customer['blocked'] === 1) {
        return reject('customer_blocked');
    }
    if ($r['operation'] === 'summary') {
        $totals = row($db, 'SELECT COUNT(*) AS count, COALESCE(SUM(amount-paid),0) AS due FROM orders WHERE tenant=:t AND customer=:c AND state NOT IN (:cancel,:refund)',
            [':t'=>$r['tenant'], ':c'=>$r['customer'], ':cancel'=>'cancelled', ':refund'=>'refunded']);
        return ['ok'=>true, 'code'=>'summary', 'orders'=>(int)$totals['count'], 'due'=>(int)$totals['due'], 'balance'=>(int)$customer['balance']];
    }
    if ($r['operation'] === 'quote' || $r['operation'] === 'place') {
        $product = row($db, 'SELECT * FROM products WHERE tenant=:t AND sku=:s',
            [':t'=>$r['tenant'], ':s'=>$r['sku']]);
        if ($product === null || (int)$product['active'] !== 1) {
            return reject('product_unavailable');
        }
        $policies = policy_functions();
        if (!array_key_exists($r['policy'], $policies)) {
            return reject('policy_missing');
        }
        if ($r['operation'] === 'quote') {
            return $policies[$r['policy']]($db, $r, $product, $customer);
        }
        $db->beginTransaction();
        try {
            $existing = row($db, 'SELECT * FROM orders WHERE tenant=:t AND id=:o',
                [':t'=>$r['tenant'], ':o'=>$r['order_id']]);
            if ($existing !== null) {
                $db->rollBack();
                if ($existing['customer'] !== $r['customer'] || $existing['sku'] !== $r['sku'] || (int)$existing['quantity'] !== $r['quantity']) {
                    return reject('idempotency_conflict');
                }
                return order_result($existing);
            }
            $quote = $policies[$r['policy']]($db, $r, $product, $customer);
            if (!$quote['ok']) {
                $db->rollBack();
                return $quote;
            }
            $changed = execute_sql($db, 'UPDATE products SET stock=stock-:q WHERE tenant=:t AND sku=:s AND stock>=:q',
                [':q'=>$r['quantity'], ':t'=>$r['tenant'], ':s'=>$r['sku']]);
            if ($changed !== 1) {
                $db->rollBack();
                return reject('stock_shortage');
            }
            if ($r['fail_at'] === 'after_stock') {
                throw new RuntimeException('injected_failure');
            }
            execute_sql($db, 'INSERT INTO orders(tenant,id,customer,sku,quantity,amount,paid,state,note,policy) VALUES(:t,:o,:c,:s,:q,:a,0,:v,:n,:p)',
                [':t'=>$r['tenant'], ':o'=>$r['order_id'], ':c'=>$r['customer'], ':s'=>$r['sku'], ':q'=>$r['quantity'], ':a'=>$quote['total'], ':v'=>'open', ':n'=>$r['note'], ':p'=>$r['policy']]);
            audit($db, $r, 'placed', $quote['total']);
            $db->commit();
        } catch (Throwable $error) {
            if ($db->inTransaction()) {
                $db->rollBack();
            }
            return reject($error->getMessage() === 'injected_failure' ? 'injected_failure' : 'db_error');
        }
        return order_result(row($db, 'SELECT * FROM orders WHERE tenant=:t AND id=:o',
            [':t'=>$r['tenant'], ':o'=>$r['order_id']]));
    }
    if (!in_array($r['operation'], ['pay','cancel','refund'], true)) {
        return reject('operation_unknown');
    }
    $db->beginTransaction();
    try {
        $order = row($db, 'SELECT * FROM orders WHERE tenant=:t AND id=:o AND customer=:c',
            [':t'=>$r['tenant'], ':o'=>$r['order_id'], ':c'=>$r['customer']]);
        if ($order === null) {
            $db->rollBack();
            return reject('order_missing');
        }
        if ($r['operation'] === 'pay') {
            if (!in_array($order['state'], ['open','partial'], true)) {
                $db->rollBack();
                return reject('not_payable');
            }
            $due = (int)$order['amount'] - (int)$order['paid'];
            $amount = $r['amount'] === -1 ? $due : $r['amount'];
            if ($amount <= 0 || $amount > $due) {
                $db->rollBack();
                return reject('payment_amount');
            }
            $paid = (int)$order['paid'] + $amount;
            $state = $paid === (int)$order['amount'] ? 'paid' : 'partial';
            execute_sql($db, 'UPDATE orders SET paid=:a,state=:s WHERE tenant=:t AND id=:o',
                [':a'=>$paid, ':s'=>$state, ':t'=>$r['tenant'], ':o'=>$r['order_id']]);
            execute_sql($db, 'INSERT INTO ledger(tenant,customer,order_id,amount,kind) VALUES(:t,:c,:o,:a,:k)',
                [':t'=>$r['tenant'], ':c'=>$r['customer'], ':o'=>$r['order_id'], ':a'=>$amount, ':k'=>'payment']);
            execute_sql($db, 'UPDATE customers SET balance=balance+:a WHERE tenant=:t AND id=:c',
                [':a'=>$amount, ':t'=>$r['tenant'], ':c'=>$r['customer']]);
            if ($r['fail_at'] === 'after_ledger') {
                throw new RuntimeException('injected_failure');
            }
            audit($db, $r, 'payment', $amount);
        } elseif ($r['operation'] === 'cancel') {
            if ($order['state'] === 'cancelled') {
                $db->rollBack();
                return order_result($order);
            }
            if ($order['state'] !== 'open' || (int)$order['paid'] !== 0) {
                $db->rollBack();
                return reject('not_cancellable');
            }
            execute_sql($db, 'UPDATE products SET stock=stock+:q WHERE tenant=:t AND sku=:s',
                [':q'=>(int)$order['quantity'], ':t'=>$r['tenant'], ':s'=>$order['sku']]);
            execute_sql($db, 'UPDATE orders SET state=:s WHERE tenant=:t AND id=:o',
                [':s'=>'cancelled', ':t'=>$r['tenant'], ':o'=>$r['order_id']]);
            audit($db, $r, 'cancel', 0);
        } else {
            if ($order['state'] === 'refunded') {
                $db->rollBack();
                return order_result($order);
            }
            if ($order['state'] !== 'paid') {
                $db->rollBack();
                return reject('not_refundable');
            }
            execute_sql($db, 'UPDATE products SET stock=stock+:q WHERE tenant=:t AND sku=:s',
                [':q'=>(int)$order['quantity'], ':t'=>$r['tenant'], ':s'=>$order['sku']]);
            execute_sql($db, 'UPDATE customers SET balance=balance-:a WHERE tenant=:t AND id=:c',
                [':a'=>(int)$order['paid'], ':t'=>$r['tenant'], ':c'=>$r['customer']]);
            execute_sql($db, 'INSERT INTO ledger(tenant,customer,order_id,amount,kind) VALUES(:t,:c,:o,:a,:k)',
                [':t'=>$r['tenant'], ':c'=>$r['customer'], ':o'=>$r['order_id'], ':a'=>-(int)$order['paid'], ':k'=>'refund']);
            execute_sql($db, 'UPDATE orders SET paid=0,state=:s WHERE tenant=:t AND id=:o',
                [':s'=>'refunded', ':t'=>$r['tenant'], ':o'=>$r['order_id']]);
            if ($r['fail_at'] === 'after_ledger') {
                throw new RuntimeException('injected_failure');
            }
            audit($db, $r, 'refund', -(int)$order['paid']);
        }
        $db->commit();
    } catch (Throwable $error) {
        if ($db->inTransaction()) {
            $db->rollBack();
        }
        return reject($error->getMessage() === 'injected_failure' ? 'injected_failure' : 'db_error');
    }
    return order_result(row($db, 'SELECT * FROM orders WHERE tenant=:t AND id=:o',
        [':t'=>$r['tenant'], ':o'=>$r['order_id']]));
}

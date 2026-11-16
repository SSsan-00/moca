function quote_@ID@(PDO $db, array $r, array $product, array $customer): array {
    if ($r['quantity'] < 1 || $r['quantity'] > @MAX_QTY@) {
        return reject('quantity_limit');
    }
    $membership = row($db, 'SELECT * FROM memberships WHERE tenant=:t AND customer=:c AND program=:p',
        [':t'=>$r['tenant'], ':c'=>$r['customer'], ':p'=>'@PROGRAM@']);
    if ($membership !== null && $membership['expires'] < $r['today']) {
        $membership = null;
    }
    if ($r['channel'] === 'wholesale' && $membership === null) {
        return reject('membership_required');
    }
    if ($product['category'] === 'restricted' && $customer['tier'] !== 'gold') {
        return reject('restricted_product');
    }
    $base = (int)$product['price'] * $r['quantity'];
    $tierRate = 0;
    $reason = 'standard-@TAG@';
    if ($customer['tier'] === 'gold') {
        $tierRate = @GOLD@;
        $reason = 'gold-@TAG@';
    } elseif ($customer['tier'] === 'silver' && $base >= @SILVER_MIN@) {
        $tierRate = @SILVER@;
        $reason = 'silver-@TAG@';
    }
    if ($r['quantity'] >= @BULK_MIN@) {
        $tierRate = max($tierRate, @BULK_RATE@);
        $reason .= ':bulk';
    }
    $discount = money_ratio($base, $tierRate, '@ROUND@');
    $coupon = $r['coupon'];
    if ($coupon !== null && $coupon !== '') {
        if ($coupon === '@COUPON@' && $r['today'] <= '@EXPIRY@') {
            if ($base >= @COUPON_MIN@ && $r['channel'] !== 'wholesale') {
                $discount += @COUPON_AMOUNT@;
                $reason .= ':coupon';
            } else {
                $reason .= ':coupon-ineligible';
            }
        } else {
            return reject('coupon_invalid');
        }
    }
    if ($membership !== null && (int)$membership['points'] >= @POINTS_MIN@) {
        $discount += @POINTS_AMOUNT@;
        $reason .= ':loyalty';
    }
    $cap = money_ratio($base, @DISCOUNT_CAP@, 'floor');
    if ($discount > $cap) {
        $discount = $cap;
        $reason .= ':capped';
    }
    $net = max(0, $base - $discount);
    $shipping = @SHIPPING@;
    if ($r['delivery'] === 'pickup') {
        $shipping = 0;
    } elseif ($r['delivery'] === 'express') {
        if ($customer['region'] === 'island' && @ISLAND_BLOCK@) {
            return reject('express_unavailable');
        }
        $shipping += @EXPRESS@;
    } elseif ($r['delivery'] !== 'standard') {
        return reject('delivery_unknown');
    }
    if ($customer['region'] === 'island' && $r['delivery'] !== 'pickup') {
        $shipping += @ISLAND@;
    }
    if ($net >= @FREE_SHIP@ && $r['delivery'] === 'standard') {
        $shipping = 0;
        $reason .= ':free-shipping';
    }
    $taxRate = $product['category'] === 'food' ? @FOOD_TAX@ : @TAX@;
    if ((int)$customer['tax_exempt'] === 1) {
        $taxRate = 0;
    }
    $tax = money_ratio($net, $taxRate, '@ROUND@');
    $fee = 0;
    if ($r['payment'] === 'cod') {
        if ($net > @COD_LIMIT@) {
            return reject('cod_limit');
        }
        $fee = @COD_FEE@;
    } elseif ($r['payment'] !== 'card' && $r['payment'] !== 'credit') {
        return reject('payment_unknown');
    }
    $total = $net + $tax + $shipping + $fee;
    if ($r['payment'] === 'credit') {
        $due = row($db, 'SELECT COALESCE(SUM(amount-paid),0) AS value FROM orders WHERE tenant=:t AND customer=:c AND state IN (:open,:partial)',
            [':t'=>$r['tenant'], ':c'=>$r['customer'], ':open'=>'open', ':partial'=>'partial']);
        if ($customer['credit_limit'] === null || (int)$due['value'] + $total > (int)$customer['credit_limit']) {
            return reject('credit_limit');
        }
    }
    return [
        'ok' => true,
        'code' => 'quoted',
        'policy' => @NUMBER@,
        'base' => $base,
        'discount' => $discount,
        'shipping' => $shipping,
        'tax' => $tax,
        'fee' => $fee,
        'total' => $total,
        'reason' => $reason,
        'member' => $membership !== null,
    ];
}

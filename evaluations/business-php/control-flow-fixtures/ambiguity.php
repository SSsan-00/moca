<?php
function truthiness(int $value): bool {
    if ($value) {
        return true;
    }
    return false;
}
function mixed(int $value, bool $flag): bool {
    return $value == $flag;
}
function shortCircuit(bool $flag): bool {
    return $flag && checkPolicy();
}
function looping(int $count): bool {
    while ($count > 0) {
        return true;
    }
    return false;
}

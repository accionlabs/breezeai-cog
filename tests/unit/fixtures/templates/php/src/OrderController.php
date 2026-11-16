<?php

namespace Shop;

class OrderController
{
    public function save(array $input): void
    {
        $db = new \PDO('sqlite::memory:');
        $stmt = $db->prepare('INSERT INTO orders (email) VALUES (?)');
        $stmt->execute([$input['email']]);
        header('Location: /orders');
    }
}

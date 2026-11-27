<?php

namespace App\Controller;

use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\Response;
use Symfony\Component\Routing\Annotation\Route;

class OrderController extends AbstractController
{
    #[Route('/orders', name: 'order_list')]
    public function list(): Response
    {
        return $this->render('orders.html.twig', ['title' => 'Orders', 'orders' => []]);
    }

    #[Route('/orders/save', name: 'order_save', methods: ['POST'])]
    public function save(): Response
    {
        return $this->redirectToRoute('order_list');
    }
}

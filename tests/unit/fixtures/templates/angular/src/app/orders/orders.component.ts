import { Component } from '@angular/core';
import { HttpClient } from '@angular/common/http';

@Component({
  selector: 'app-orders',
  templateUrl: './orders.component.html',
})
export class OrdersComponent {
  orders: { id: number; total: number }[] = [];
  email = '';

  constructor(private http: HttpClient) {}

  save(): void {
    this.http.post('/api/orders', { email: this.email }).subscribe();
  }

  remove(id: number): void {
    this.http.delete(`/api/orders/${id}`).subscribe();
  }
}

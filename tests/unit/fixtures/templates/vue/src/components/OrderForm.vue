<template>
  <form @submit.prevent="save">
    <label for="email">E-mail</label>
    <input id="email" v-model="email" placeholder="E-mail" />
    <el-input v-model="note" />
    <OrderRow v-for="order in orders" :key="order.id" :order="order" @remove="remove" />
    <button type="submit">Save</button>
  </form>
  <p v-if="saved">{{ message }}</p>
  <router-link to="/orders">Back</router-link>
  <a href="/help.html">Help</a>
</template>

<script>
import axios from 'axios';
import OrderRow from './OrderRow.vue';

export default {
  name: 'OrderForm',
  components: { OrderRow },
  data() {
    return { email: '', note: '', orders: [], saved: false, message: '' };
  },
  methods: {
    save() {
      axios.post('/api/orders', { email: this.email });
    },
    remove(id) {
      axios.delete(`/api/orders/${id}`);
    },
  },
};
</script>

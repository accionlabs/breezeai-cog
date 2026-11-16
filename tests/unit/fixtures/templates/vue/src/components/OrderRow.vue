<template>
  <div>{{ order.id }} <button @click="$emit('remove', order.id)">Delete</button></div>
</template>

<script>
export default {
  name: 'OrderRow',
  props: ['order'],
};
</script>

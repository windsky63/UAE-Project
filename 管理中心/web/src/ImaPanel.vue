<script setup>
import ImaWorkspace from './ImaWorkspace.vue'
defineProps({ api: { type: Function, required: true } })
const emit = defineEmits(['notice'])
</script>

<template>
  <ImaWorkspace :api="api" @notice="(message, error) => emit('notice', message, error)" />
</template>

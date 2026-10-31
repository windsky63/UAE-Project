<script setup>
defineProps({ text: String, label: { type: String, default: '查看说明' } })
</script>

<template>
  <v-tooltip :text="text" location="top" max-width="360" open-on-focus open-on-click>
    <template #activator="{ props }">
      <v-btn v-bind="props" :aria-label="label" icon="mdi-information-outline" variant="text" color="secondary" size="x-small" class="help-tip" />
    </template>
  </v-tooltip>
</template>

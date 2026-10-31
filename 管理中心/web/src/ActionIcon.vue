<script setup>
defineOptions({ inheritAttrs: false })
defineProps({
  label: { type: String, required: true },
  icon: { type: String, required: true },
  color: { type: String, default: 'primary' },
  disabled: Boolean,
})
</script>

<template>
  <v-tooltip :text="label" location="top" open-on-focus open-on-click>
    <template #activator="{ props }">
      <span v-bind="props" class="action-icon-anchor" :tabindex="disabled ? 0 : undefined" :aria-label="disabled ? label : undefined">
        <v-btn v-bind="$attrs" :aria-label="label" :icon="icon" :color="color" :disabled="disabled" variant="tonal" size="small" rounded="lg" class="table-action-icon" />
      </span>
    </template>
  </v-tooltip>
</template>

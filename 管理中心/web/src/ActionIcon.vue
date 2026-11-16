<script setup>
defineOptions({ inheritAttrs: false })
defineProps({
  label: { type: String, required: true },
  icon: { type: String, required: true },
  color: { type: String, default: 'primary' },
  disabled: Boolean,
  variant: { type: String, default: 'tonal' },
  size: { type: String, default: 'small' },
})
</script>

<template>
  <v-tooltip :text="label" location="top" max-width="360" open-on-focus class="action-tooltip">
    <template #activator="{ props }">
      <span v-bind="props" @focusin="props.onFocus?.($event)" @focusout="props.onBlur?.($event)" class="action-icon-anchor" :tabindex="disabled ? 0 : undefined" :aria-label="disabled ? label : undefined">
        <v-btn v-bind="$attrs" :aria-label="label" :icon="icon" :color="color" :disabled="disabled" :variant="variant" :size="size" rounded="lg" class="table-action-icon" />
      </span>
    </template>
  </v-tooltip>
</template>

<style>
.action-tooltip{overflow:clip}
.action-tooltip .v-overlay__content{overflow-wrap:anywhere}
</style>

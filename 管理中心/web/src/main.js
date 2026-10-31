import { createApp } from 'vue'
import App from './App.vue'
import 'vuetify/styles'
import '@mdi/font/css/materialdesignicons.css'
import { createVuetify } from 'vuetify'
import {
  VApp, VMain, VContainer, VRow, VCol, VAvatar, VIcon, VChip, VCard, VCardActions,
  VBtn, VForm, VTextField, VTextarea, VSelect, VSnackbar, VNavigationDrawer, VCardItem, VCardText,
  VDivider, VList, VListItem, VAppBar, VAppBarNavIcon, VAppBarTitle, VTable,
  VSpacer, VDialog, VMenu, VTooltip, VProgressLinear, VExpansionPanels, VExpansionPanel,
  VExpansionPanelText, VProgressCircular, VSwitch, VCombobox, VCheckbox, VAlert, VTimePicker,
} from 'vuetify/components'
import { zhHans } from 'vuetify/locale'
import * as directives from 'vuetify/directives'
import './style.css'

const vuetify = createVuetify({
  components: {
    VApp, VMain, VContainer, VRow, VCol, VAvatar, VIcon, VChip, VCard, VCardActions,
    VBtn, VForm, VTextField, VTextarea, VSelect, VSnackbar, VNavigationDrawer, VCardItem, VCardText,
    VDivider, VList, VListItem, VAppBar, VAppBarNavIcon, VAppBarTitle, VTable,
    VSpacer, VDialog, VMenu, VTooltip, VProgressLinear, VExpansionPanels, VExpansionPanel,
    VExpansionPanelText, VProgressCircular, VSwitch, VCombobox, VCheckbox, VAlert, VTimePicker,
  },
  directives,
  locale: { locale: 'zhHans', fallback: 'en', messages: { zhHans } },
  theme: {
    defaultTheme: 'assistant',
    themes: { assistant: { dark: false, colors: {
      primary: '#245bd6', secondary: '#56677f', background: '#f4f6fa', surface: '#ffffff',
      'surface-light': '#f1f6ff', 'on-surface': '#172b4d', 'on-background': '#172b4d',
      error: '#dc4545', success: '#16836a', info: '#2563eb',
    } } },
  },
  defaults: {
    VBtn: { rounded: 'lg', elevation: 0 },
    VCard: { rounded: 'lg', elevation: 0, border: true },
    VTextField: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto', color: 'primary' },
    VTextarea: { variant: 'outlined', hideDetails: 'auto', color: 'primary' },
    VSelect: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto', color: 'primary' },
    VCombobox: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto', color: 'primary' },
    VTooltip: { openDelay: 150 },
    VProgressCircular: { size: 20, width: 2 },
    VSwitch: { inset: true, size: 'small', density: 'compact', hideDetails: true, color: 'primary', trueIcon: 'mdi-check', falseIcon: 'mdi-minus' },
  },
})

createApp(App).use(vuetify).mount('#app')

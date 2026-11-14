import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './smart-planner.css'
import './native.css'
import { Capacitor } from '@capacitor/core'

if (Capacitor.getPlatform() === 'ios') document.documentElement.classList.add('native-ios')

createApp(App).mount('#app')

import React from 'react'
import PluginAppComponent from './PluginApp'

export const PluginApp: React.ComponentType = PluginAppComponent

export const pluginRoutes = [
  {
    path: '/monitoring',
    label: 'State Monitoring',
    icon: 'monitor_heart',
    sidebar_visible: true,
  },
]

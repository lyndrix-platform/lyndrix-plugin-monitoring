import PluginApp from './PluginApp'

// Explicit global assignment — more reliable than relying on Rollup's IIFE
// named-export → window property mechanism in strict-mode bundles (mirrors the
// reference docker-manager plugin). The host (usePluginModules) reads
// window.__lyndrix_plugin_<safeId> where safeId = id with [.-] → _.
;(window as unknown as Record<string, unknown>)['__lyndrix_plugin_lyndrix_plugin_state_monitoring'] = {
  PluginApp,
  pluginRoutes: [
    {
      path: '/monitoring',
      label: 'State Monitoring',
      icon: 'monitor_heart',
      sidebar_visible: true,
    },
  ],
}

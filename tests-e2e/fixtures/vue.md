# Vue {VERSION}

Vue {VERSION} is a progressive JavaScript framework for building user
interfaces with reactive components and single-file components.

## Getting Started

The easiest way to try Vue {VERSION} is the CDN build. Create an
`index.html` file and mount your first app in seconds.

```js
import { createApp } from 'vue'

createApp({
  data() {
    return { version: '{VERSION}' }
  }
}).mount('#app')
```

## Reactivity

Vue {VERSION} tracks JavaScript state changes and efficiently updates the
DOM when changes happen, using a fine-grained reactivity system.

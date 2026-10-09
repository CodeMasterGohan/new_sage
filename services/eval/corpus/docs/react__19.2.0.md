# React 19.2.0

## useEffect

useEffect is a React Hook that lets you synchronize a component with an external system, like fetching data from a network. Call it at the top level of your component: useEffect(setup, dependencies?).

```jsx
import { useEffect, useState } from 'react';

function UserProfile({ userId }) {
  const [user, setUser] = useState(null);
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/users/${userId}`)
      .then((r) => r.json())
      .then((data) => { if (!cancelled) setUser(data); });
    return () => { cancelled = true; };
  }, [userId]);
  if (!user) return <p>Loading…</p>;
  return <h1>{user.name}</h1>;
}
```

## What's new in React 19: ref as a prop

React 19 adds support for ref as a regular prop in function components. You no longer need React.forwardRef: function MyInput({ ref }) { return <input ref={ref} /> } works directly, and forwardRef is now deprecated.

```jsx
// React 19: no forwardRef needed
function MyInput({ ref, ...props }) {
  return <input ref={ref} {...props} />;
}

function Form() {
  const inputRef = useRef(null);
  return <MyInput ref={inputRef} placeholder="Type here" />;
}
```

## useEffect parameters

The signature is useEffect(setup, dependencies?). The setup function contains the Effect's logic and may return a cleanup function; dependencies is an optional array of reactive values — every value referenced inside setup must be listed.

## useState

useState is a React Hook that lets you add a state variable to your component. Call useState at the top level with the initial state: const [state, setState] = useState(initialState). The set function replaces the state on the next render.

## useMemo

useMemo is a React Hook that lets you cache the result of a calculation between re-renders. Use it to skip expensive recalculations when dependencies haven't changed: const visibleTodos = useMemo(() => filterTodos(todos, tab), [todos, tab]).

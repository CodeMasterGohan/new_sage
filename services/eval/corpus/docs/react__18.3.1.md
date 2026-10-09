# React 18.3.1

## forwardRef

In React 18, function components cannot receive a ref prop directly. Wrap the child with React.forwardRef((props, ref) => ...) and attach the received ref to a DOM node or another component inside.

```jsx
import { forwardRef, useRef } from 'react';

const MyInput = forwardRef(function MyInput(props, ref) {
  return <input {...props} ref={ref} />;
});

function Form() {
  const inputRef = useRef(null);
  return (
    <>
      <MyInput ref={inputRef} />
      <button onClick={() => inputRef.current.focus()}>Focus</button>
    </>
  );
}
```

## useCallback parameters

The signature is useCallback(fn, dependencies). The fn argument is the function value you want to cache, and dependencies is the list of reactive values referenced inside fn; it returns the cached fn.

## useRef

useRef is a React Hook that lets you reference a value that's not needed for rendering. Declare it with const ref = useRef(initialValue); mutating ref.current does not trigger a re-render, which makes refs ideal for storing DOM nodes and timeout IDs.

## StrictMode

StrictMode lets you find common bugs in your components early during development. It enables extra development-only checks: double-invoking Effects, warning about deprecated APIs, and verifying that components are pure. Wrap your app root: <StrictMode><App /></StrictMode>.

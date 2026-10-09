# TypeScript 5.9.2

## Union types

Separate the members with |: type ID = string | number. A value of type ID may be a string or a number, and TypeScript narrows the union with typeof checks.

```ts
type ID = string | number;

function printId(id: ID) {
  if (typeof id === "string") {
    console.log(id.toUpperCase());
  } else {
    console.log(id.toFixed(2));
  }
}
```

## What's new in TypeScript 5.5: inferred type predicates

TypeScript 5.5 infers type predicates automatically: arr.filter(x => x !== undefined) now narrows the result type without an explicit x is T predicate. The inference applies to common predicate-shaped callbacks.

## Array.prototype.map

The signature is map<U>(callbackfn: (value: T, index: number, array: T[]) => U, thisArg?: any): U[]. It calls callbackfn once per element and returns a new array of the results.

## Interfaces

Interfaces describe object shapes: interface User { name: string; age?: number }. Use extends to inherit: interface Admin extends User { permissions: string[] }. Prefer interface for public API shapes and type for unions and tuples.

## Generics

Generics parameterize types: function identity<T>(arg: T): T { return arg; }. Constrain with extends: function longest<T extends { length: number }>(a: T, b: T): T.

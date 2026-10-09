# TypeScript 5.8.3

## Partial

Wrap the type: type PartialUser = Partial<User>. Partial<T> constructs a type with all properties of T set to optional, so each property may be present or omitted.

```ts
interface User {
  name: string;
  age: number;
}

function updateUser(id: string, patch: Partial<User>) {
  // patch may contain name, age, both, or neither
}
```

## Record

The signature is Record<Keys, Type>. Keys is the union of property keys and Type is the property value type, for example Record<string, number> maps string keys to numbers.

## Enums

Enums give friendly names to sets of values: enum Direction { Up, Down, Left, Right }. Prefer union types of string literals ("up" | "down") in new code — they erase cleanly and compose better.

## Narrowing

TypeScript narrows unions through control flow: after if (typeof x === "string"), x is string in that branch. Discriminated unions narrow on a literal tag: if (shape.kind === "circle") gives access to shape.radius.

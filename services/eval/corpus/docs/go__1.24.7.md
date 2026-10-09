# Go 1.24.7

## gofmt

Run gofmt -l -w . from the module root: -w rewrites files in place and -l lists files whose formatting differs. gofmt formats code to the canonical style; go vet catches suspicious constructs instead.

## What's new in Go 1.24: tool dependencies

Go 1.24 added the tool directive in go.mod for declaring tool dependencies, plus go tool <name> to run them. This replaces the tools.go file with a blank import that older Go versions required.

## net/http.HandleFunc

The signature is func HandleFunc(pattern string, handler func(ResponseWriter, *Request)). It registers handler on the DefaultServeMux for the given URL pattern.

```go
package main

import "net/http"

func main() {
    http.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) {
        w.Write([]byte("hello"))
    })
    http.ListenAndServe(":8080", nil)
}
```

## Slices

The slices package covers common operations: slices.Sort(nums), slices.Contains(nums, 3), slices.Compact removes consecutive duplicates. Prefer slices over hand-rolled loops since Go 1.21.

## Errors

Return errors as values: if err != nil { return fmt.Errorf("load config: %w", err) }. Use errors.Is/As to inspect wrapped chains; sentinel errors are package-level vars like io.EOF.

# Go 1.25.1

## Tool directives

Add a tool directive: tool golang.org/x/tools/cmd/stringer. Go 1.24 added tool directives so tool dependencies are tracked in go.mod and installed with go tool, replacing the old tools.go trick.

```go
// go.mod
module example.com/myapp

go 1.24

tool golang.org/x/tools/cmd/stringer
```

Run a tracked tool with: go tool stringer -type=Pill .

## fmt.Sprintf

The signature is func Sprintf(format string, a ...any) string. It formats according to the format string and returns the resulting string instead of printing it.

```go
s := fmt.Sprintf("hello, %s (n=%d)", name, n)
```

## Modules

Initialize with go mod init example.com/myapp; add dependencies with go get golang.org/x/text@latest. The go.mod file records module requirements and the go directive sets the minimum toolchain version.

## Testing

Tests live in _test.go files: func TestAdd(t *testing.T) { if Add(1, 2) != 3 { t.Fatal("wrong") } }. Run go test ./... for the whole module; table-driven tests iterate over a slice of input/expected pairs.

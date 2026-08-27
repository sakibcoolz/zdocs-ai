# Test fixture repositories

Small, self-contained repositories used by the offline test suite and by
`make analyze-sample`. Each covers one supported language and deliberately
includes the constructs the analyzers are expected to detect: abstract types,
interfaces, inheritance, interface implementation, composition and dependency
injection.

| Directory | Language | Notable constructs |
| --- | --- | --- |
| `python_repo/` | Python | `ABC`, `Protocol`, inheritance, composition, ctor injection |
| `go_repo/` | Go | interface, struct embedding, structural satisfaction, a *partial* implementer that must NOT be reported |
| `java_repo/` | Java | `interface extends`, `abstract class`, `implements`, `@Override`, ctor injection |
| `ts_repo/` | TypeScript | `interface extends`, `abstract class`, `implements`, parameter properties, `#private` |
| `js_repo/` | JavaScript | ES class inheritance, `require`, method override |

These are fixtures, not examples of good code. Keep them small: every test that
counts symbols depends on their exact contents.

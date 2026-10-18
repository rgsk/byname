# Feedback: world-model-final

Claude (Opus 5.5) building `projects/world-model-final` in byname, 2026-10-18: a flow-matching world
model, plus latent actions learned from frames alone.

## Bug: a record literal holding a function fails the checker in another module

A record with a `Callable` field accepts a literal written in the module that defines it, but rejects
the same literal written in another module:

```python
# a.pyn
from collections.abc import Callable
from torch import Tensor

type Fn = (fn: Callable[[Tensor], Tensor], name: str)

def takes_fn(f: Fn) -> None: ...
```

```python
# b.pyn
from torch import Tensor
from a import takes_fn

def double(x: Tensor) -> Tensor:
    return 2 * x

takes_fn((fn=double, name="a"))
# error: Argument of type "(fn: (x: Tensor) -> Tensor, name: str)" cannot be assigned to parameter "f"
```

Moving the call into `a.pyn` gives 0 errors. Only functions are affected. Across modules, these pass:

```python
type Scaled = (factor: float, name: str)
type Wrapped = (net: nn.Module, name: str)

takes_scaled((factor=2, name="a"))                # int into float: ok
takes_wrapped((net=nn.Linear(1, 1), name="a"))    # subclass into nn.Module: ok
```

The checker's explanation, from a run with `type Latent = (codes_of: Callable[[Tensor, Tensor],
Tensor], key_codes: Tensor)`:

```
"(codes_of: (before: Tensor, key_codes: after: Tensor) -> Tensor)" is incompatible with protocol
"(codes_of: (Tensor, key_codes: Tensor) -> Tensor)"
  "_replace" is an incompatible type
    Keyword parameter "codes_of" of type "(Tensor, Tensor) -> Tensor" is incompatible with type
    "(before: Tensor, after: Tensor) -> Tensor"
```

**Likely cause:** the full message in the editor (cut off in the terminal) starts with

```
"(fn: (x: Tensor) -> Tensor, name: str)" is incompatible with protocol "(fn: (Tensor) -> Tensor, name: str)"
  Could not bind method "double" because "(fn: (x: Tensor) -> Tensor, name: str)" is not assignable
  to parameter "x"
```

So when the record is checked as a protocol, its `Callable`-typed field is treated as a **method**,
and the checker binds the record instance as the function's first argument. Declaring callable fields
on the protocol so they aren't methods (e.g. as a `ClassVar`-free attribute the checker won't bind,
or through a property) may fix it. The `_replace` mismatch below looks like a knock-on effect.

The literal also keeps the function's exact signature, with named parameters, which is narrower than
the field's `Callable`. Records are then compared through `_replace`, whose parameters are contravariant,
so the wider declared type doesn't fit the literal's narrower one.

Two things made it slow to find:
- The displayed type merges the function's parameter list into the record's
  (`key_codes: after: Tensor`). That looked at first as if a comment inside the record was being
  parsed.
- An annotated local, `codes_of: Callable[[Tensor, Tensor], Tensor] = plain`, doesn't help, because
  the local narrows back to `plain`'s exact type.

**Workaround used:** a protocol field holding the object, not its method. That passes across modules:

```python
class Coder(Protocol):
    def codes(self, before: Tensor, after: Tensor) -> Tensor: ...

type Latent = (coder: Coder, key_codes: Tensor)

train(model, train_cfg=, latent=(coder=latent_model, key_codes=))   # in another module: ok
```

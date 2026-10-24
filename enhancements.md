# Enhancements

Touch-ups: things that work, but could match TypeScript more closely. Not bugs.

## Hover on a literal's field shows the generated stub's parameter

```python
it: Item = (sku="a")   # hover on sku: "(parameter) sku: str"
```

The checker sees the literal as a call to its `_rec_` stub, so hover calls the field a parameter. TypeScript
shows the type's property there. Cosmetic: the type is right. The proxy could rewrite `(parameter)` to
`(field)` when the hover is on a literal's field name (`Result.fields` has their spans).

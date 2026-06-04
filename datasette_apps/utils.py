def sort_names_with_underscores_last(names):
    return sorted(names, key=lambda name: (name.startswith("_"), name.lower()))

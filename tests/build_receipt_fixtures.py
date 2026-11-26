from hashlib import sha256


def saved_build_receipt():
    # The receipt first persisted by a4e27af has the same fields as the current writer.
    def digest(label):
        return sha256(label.encode()).hexdigest()

    return {
        "inputs": {
            "project": "Example.csproj", "framework": "net9.0", "dotnet": "C:/fixture/dotnet.exe",
            **{name: digest(name) for name in ("source", "environment", "launcher", "framework_packs")},
        },
        "artifacts": {name: digest(name) for name in ("bin", "obj")},
    }

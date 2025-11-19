# Minecraft Server Installer

This is a utility written in python that allows creating server manifests declaring plugins and other assets for easy share and install.

**Navigation:**:
* 🔧 [Installation, running, and usage](#installation-and-running)  
* ✏ [Creating manifests](#writing-you-own-manifest)

# Manifest

A manifest is a text file in which the user describes the list of assets using providers system.<br>
Manifest consists of several fields:
- `mc_version`: Minecraft version that server uses, for example `1.21.8`
- `core`: declaration of server core to use
- `mods`: list of mod asset declarations. Placed into `mods` folder
- `plugin`: list of plugin asset declarations. Placed into `plugins` folder
- `datapacks`: list of datapack asset declarations. Placed into `world/datapacks` folder
- `customs`: list of custom asset declarations. Folder where they will be placed is configured manually

### Asset
An asset is a unit representing a mod, plugin, datapack, or custom item.
Each asset has a:
- `type` that declares which provider to use
- `file_selector` - key or object that defines which files are downloaded by provider
- `asset_id` - a unique ID for this asset, automatically generated based on type-specific properties if not set manually
- `caching` - Enables or disables caching for this asset
- `actions` - List of actions to execute on downloaded data for correct installation using expression templates
- `folder` - folder where asset will be downloaded. Used only by custom assets

Example asset declaring to download latest version of [ProtocolLib](https://github.com/dmulloy2/ProtocolLib) from github and rename downloaded file to ProtocolLib-[version].jar:
```json5
{
  type: "github",
  repository: "dmulloy2/ProtocolLib",
  version: "latest",
  actions: [
    {
      type: "rename",
      to: "ProtocolLib-${{data.tag_name}}.jar"
    }
  ]
}
```

### Provider
A provider is a service or method used to download assets and check for updates.

Currently available providers:
- `modrinth`: Downloads assets from https://modrinth.com
- `github`: Downloads assets from Github releases
- `github-actions`: Downloads assets from Github actions
- `url`: Downloads single file from some HTTP url
- `jenkins`: Downloads assets from specified jenkins job

There is also a special provider called `note`. It does not have any installation or update checking methods so its only purpose it to log some notice to end user, for example, telling manual installation instructions for plugin that cannot be downloaded using available providers.

### Actions
Action is an operation that runs after downloading file and can be used to perform some installation steps. It uses an expression templates to insert values into text.

For example, you can rename downloaded file:
```json5
{
    type: "rename",
    to: "ProtocolLib-${{data.tag_name}}.jar"
}
```

Above example is used in pair with `github` provider to rename `ProtocolLib.jar` to `ProtocolLib-<version>.jar`

Each action has an optional `if` field which can contain python expression (don't confuse with template expressions) that returns boolean value. When it specified, action will be run only if check if passed. For example, this action will log `Hello world!` if there are only single file downloaded:
```json5
{
    type: "dummy",
    if: "len(data.files) == 1",
    expr: "'Hello world!'"
}
```

#### Action types:
Currently, there are 3 action types which you can use:  
- `dummy` - Logs expression result into install log that can be seen while installing.  
- `rename` - Renames primary file of data to specified file name
- `unzip` - Unpacks primary file into specified folder. If folder is not set, unpacks archive to same folder where downloaded file is located. Supports `.zip`



#### Template expressions
Template expressions are a flexible and simple way to insert values into text. Inspired by Github Actions, expressions are enclosed in `${{` and `}}` characters. When needed, you can escape expression by adding `\` before `$`.<br>
Expressions in brackets must be valid python code that gives some value that will be inserted into text.

There are 4 variables that is exposed in template expressions used in `actions` list:  
- `data` (`d`) - is an object representing the data downloaded by the provider. Different providers have different data types. For example, `github` provider has `GithubReleaseData` which has `repo` and `release` fields which is objects from [PyGithub](https://pypi.org/project/PyGithub/) library. Also each data type has `files`, `primary_files` and `first_file` properties that contains downloaded file path(s) relative to server folder.
- `asset` (`a`) - Is an object mirroring asset declaration that specified in manifest 
- `env` - is an object representing `Environment` data class. It has current profile (`profile`), authorization data (`auth`), registry access (`registries`) and `debug` flag.
- `profile` - shortcut to `env.profile`

# Installation and running
There are 3 `.pyz` files in each release and you need to pick most closest to you:
- `mcsi-ver.pyz`: A pure python archive, without any dependencies. Instead, dependencies from [pyproject.toml](pyproject.toml) must be installed
- `mcsi-ver-win.pyz`: A python archive that contains dependencies to work on Windows
- `mcsi-ver-linux.pyz`: A python archive that contains dependencies to work on Linux-like systems (e.g Ubuntu or Debian)

To run this files you need to have Python 3.10 or newer installed on your computer.

## Usage
Command syntax is `python mcsi-ver-os.pyz <command> <args>`.

Currently there are 4 commands, each doing a set of actions:
- `install`: Primary command used to install server. It searches for manifest file in current folder (or uses specified using args) and installs server to specified folder (defaults to current folder)
- `update`: Uses installation cache for check asset for updates. Dry mode can be enabled to perform update check without installation.
- `schema`: Can be used to generate `manifest_schema.json`
- `dump`: Can be used to dump all registry entries into JSON file (default is `registries.json`)

You can see usage for any command by adding `--help` argument

Example minimal command:  
`python mcsi-2.2.0-win.pyz install --manifest myserver.json5`  
This command above uses `myserver.json5` manifest file to install server into current folder.

# Writing you own manifest
The primary requirements is knowledge of language in which manifest is written, this docs and having an IDE, like Visual Studio Code.

## Selecting language
The first step is to choose a language in which you will write the manifest:

Currently there are several languages supported that can be used to write manifest:
- `JSON`
- `JSONC`: JSON with comments
- `JSON5`: Easy and flexible JSON, examples used here are written in it.
- `YAML`: `.yml` or `.yaml` files

Then you can create a document with that extension and open it in your IDE

## Attaching schema
A schema is a description of the document used by the IDE to provide you documentation on available fields, values and options

To attach a schema you must find the link to `manifest_schema` and insert it into document in place supported by your IDE. The most common way is using JSON-like language:
```json5
{
    $schema: "https://raw.githubusercontent.com/BoBkiNN/mc-server-installer/refs/tags/2.2.0/mcsi/manifest_schema.json"
    // rest of your manifest
}
```

## Actual writing
Now, once you have attached a schema, you can use IDE suggestions to fill you manifest

# TODO
Planned features and improvements for this project:
- Better way of passing authorization data and using it in assets
- Provide example manifest
- Hangar support
- asset configurations with YML/JSON/TOML support
- concurrent installing and update checking
- resolve asset dependencies
- store data version in manifest and cache for later migration support
- rename registries to something like action/model action/handler

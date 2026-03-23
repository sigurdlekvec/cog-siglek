# InField Deployment Notes

## Current Status

### Completed
- ✅ Added `modules/cdf_demo_infield` to `config.dev.yaml` selected modules
- ✅ Added InField configuration variables structure in `config.dev.yaml`
- ✅ Prepared configuration structure for IdP group mapping

### Pending

#### 1. Module Files Installation
**Status:** ⚠️ **BLOCKER** - Module files are required before proceeding

The `cdf_demo_infield` module files must be present in:
```
cog-siglek/modules/cdf_demo_infield/
```

**Required module structure:**
- `auth/` - Group definitions
- `cdf_applications/` - InField application configuration
- `data_models/` or `data_modeling/` - Data model definitions
- Other resource directories as needed

**How to obtain:**
- Contact Cognite Support for access to the module repository
- Check Cognite documentation for module download instructions
- Use `cdf modules init` if it provides access to official modules

#### 2. Identity Provider (IdP) Group Collection
**Status:** ⏳ **Manual Step Required**

Collect Object IDs from Microsoft Entra ID (Azure AD) for the following groups:

##### Required Groups:

1. **Toolkit Groups** (for deployment):
   - `cdf_tk_dev_readwrite_all` - Toolkit deployment access
   - `cdf_tk_dev_readonly_all` - Toolkit read-only access

2. **Application Configuration Group**:
   - `infield_dev_application_configuration` - InField admins (all locations)

3. **Per-Location Groups** (create for each location):
   - `infield_dev_<location>_checklist_admins` - Checklist administrators
   - `infield_dev_<location>_normal_users` - Regular users
   - `infield_dev_<location>_template_admins` - Template administrators
   - `infield_dev_<location>_viewers` - Read-only viewers

**Steps to collect:**
1. Log in to Azure Portal (portal.azure.com)
2. Navigate to Microsoft Entra ID > Groups
3. Find or create the required groups
4. Copy the **Object ID** (not Display Name) for each group
5. Update `config.dev.yaml` with the Object IDs

#### 3. Configuration Updates
**Status:** ⏳ **Pending Module Files**

Once module files are available:
1. Review the module's variable requirements
2. Update variable names in `config.dev.yaml` to match module expectations
3. Replace all `<change_me>` placeholders with actual IdP group Object IDs

#### 4. Build and Deploy
**Status:** ⏳ **Pending Module Files**

After module files are installed and IdP groups are configured:
1. Run `cdf build --env=dev` to build the InField module
2. Run `cdf deploy --env=dev --dry-run` to preview deployment
3. Run `cdf deploy --env=dev` to deploy to `siglekdogfood` project

## InField Groups Reference

Based on Cognite documentation, the following groups will be created in CDF:

| Group Name | Purpose |
|------------|---------|
| `cdf_tk_dev_readwrite_all` | Programmatic access to provision and update services |
| `cdf_tk_dev_readonly_all` | Programmatic access to list or read services |
| `infield_dev_application_configuration` | View InField configuration across all locations |
| `infield_dev_<location>_checklist_admins` | Admin users for checklists in specific location |
| `infield_dev_<location>_normal_users` | Regular users for checklists in specific location |
| `infield_dev_<location>_template_admins` | Admin users for templates in specific location |
| `infield_dev_<location>_viewers` | View-only access to checklists in specific location |

## Next Steps

1. **Obtain module files** - Contact Cognite Support or check documentation
2. **Create/collect IdP groups** - Set up groups in Azure AD and collect Object IDs
3. **Update configuration** - Replace `<change_me>` values with actual Object IDs
4. **Build and deploy** - Follow standard toolkit deployment process

## References

- [Setting up InField](https://docs.cognite.com/cdf/deploy/cdf_toolkit/guides/set_up_infield)
- [Configure data models for InField](https://docs.cognite.com/cdf/infield/guides/config_idm)
- [Configure, build, and deploy modules](https://docs.cognite.com/cdf/deploy/cdf_toolkit/guides/usage)

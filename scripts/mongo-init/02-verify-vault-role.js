const databaseName = process.env.PORTAL_VAULT_MONGODB_DATABASE;

if (!databaseName) {
    throw new Error("Portal vault database is not configured.");
}

const vaultDatabase = db.getSiblingDB(databaseName);
const runtimeRole = vaultDatabase.getRole(
    "portalVaultRuntime",
    { showPrivileges: true },
);
if (!runtimeRole) {
    throw new Error("Portal vault runtime role is missing.");
}

const forbiddenActions = new Set([
    "anyAction",
    "dropCollection",
    "dropDatabase",
    "remove",
]);
for (const privilege of runtimeRole.privileges) {
    for (const action of privilege.actions) {
        if (forbiddenActions.has(action)) {
            throw new Error(`Forbidden portal vault runtime action: ${action}`);
        }
    }
}

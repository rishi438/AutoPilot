const databaseName = process.env.PORTAL_VAULT_MONGODB_DATABASE;
const username = process.env.PORTAL_VAULT_APP_USERNAME;
const password = process.env.PORTAL_VAULT_APP_PASSWORD;

if (!databaseName || !username || !password) {
    throw new Error("Portal vault application identity is not configured.");
}

const vaultDatabase = db.getSiblingDB(databaseName);
for (const collectionName of ["portal_credentials", "portal_credential_events"]) {
    if (!vaultDatabase.getCollectionNames().includes(collectionName)) {
        vaultDatabase.createCollection(collectionName);
    }
}
const runtimeRole = {
    role: "portalVaultRuntime",
    privileges: [
        {
            resource: { db: databaseName, collection: "portal_credentials" },
            actions: ["find", "insert", "update", "listIndexes", "createIndex"],
        },
        {
            resource: { db: databaseName, collection: "portal_credential_events" },
            actions: ["insert", "listIndexes", "createIndex"],
        },
    ],
    roles: [],
};
if (vaultDatabase.getRole(runtimeRole.role)) {
    vaultDatabase.updateRole(
        runtimeRole.role,
        { privileges: runtimeRole.privileges, roles: runtimeRole.roles },
    );
} else {
    vaultDatabase.createRole(runtimeRole);
}
const runtimeRoles = [{ role: runtimeRole.role, db: databaseName }];
if (vaultDatabase.getUser(username)) {
    vaultDatabase.updateUser(username, { pwd: password, roles: runtimeRoles });
} else {
    vaultDatabase.createUser({ user: username, pwd: password, roles: runtimeRoles });
}

import androidVersion from '../android/version.json'

export const APK_VERSION_NAME = androidVersion.versionName
export const APK_VERSION_CODE = androidVersion.versionCode
export const APK_VERSION_LABEL = `APK ${APK_VERSION_NAME} · code ${APK_VERSION_CODE}`

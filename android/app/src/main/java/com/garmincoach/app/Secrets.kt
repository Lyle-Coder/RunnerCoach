package com.garmincoach.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * 账号密码的保存。
 *
 * 为什么必须加密：佳明的会话（JWT_WEB）只有 2 小时，想让「点一下同步」可用，
 * 就得能自动续期；能自动续期就得留着密码。明文放在 SharedPreferences 里，
 * 任何拿到 root 或备份的 App 都能读走——那是能登进他人健康档案的凭据。
 *
 * 这里用 Android Keystore 里的 AES-GCM 密钥加密，密钥不出 TEE/StrongBox，
 * 拷走 xml 文件也解不开（换台设备就失效，正好）。
 */
class Secrets(ctx: Context) {

    private val prefs = ctx.getSharedPreferences("gc_secrets", Context.MODE_PRIVATE)

    fun email(): String? = prefs.getString("email", null)

    fun password(): String? = decrypt("iv", "pwd")

    fun save(email: String, password: String) = saveTo("email", "iv", "pwd", email, password)

    // 高驰：与佳明同一套 Keystore 加密，键名前缀区分，互不清空
    fun corosEmail(): String? = prefs.getString("coros_email", null)

    fun corosPassword(): String? = decrypt("coros_iv", "coros_pwd")

    fun saveCoros(email: String, password: String) =
        saveTo("coros_email", "coros_iv", "coros_pwd", email, password)

    fun clearCoros() {
        prefs.edit()
            .remove("coros_email").remove("coros_iv").remove("coros_pwd")
            .apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
    }

    private fun decrypt(ivKey: String, ctKey: String): String? {
        val iv64 = prefs.getString(ivKey, null) ?: return null
        val ct64 = prefs.getString(ctKey, null) ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORM)
            cipher.init(
                Cipher.DECRYPT_MODE,
                key(),
                GCMParameterSpec(128, Base64.decode(iv64, Base64.NO_WRAP)),
            )
            String(cipher.doFinal(Base64.decode(ct64, Base64.NO_WRAP)), Charsets.UTF_8)
        } catch (t: Throwable) {
            null            // 密钥被清（换机/恢复出厂）就当没存过，让用户重登
        }
    }

    private fun saveTo(emailKey: String, ivKey: String, ctKey: String,
                       email: String, password: String) {
        try {
            val cipher = Cipher.getInstance(TRANSFORM)
            cipher.init(Cipher.ENCRYPT_MODE, key())
            val ct = cipher.doFinal(password.toByteArray(Charsets.UTF_8))
            prefs.edit()
                .putString(emailKey, email)
                .putString(ivKey, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
                .putString(ctKey, Base64.encodeToString(ct, Base64.NO_WRAP))
                .apply()
        } catch (t: Throwable) {
            // 存不了就只存邮箱：下次同步会要求重新输入密码，功能不中断
            prefs.edit().putString(emailKey, email).apply()
        }
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }

        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        gen.init(
            KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                // 不要求解锁才可用：跑步 App 常在锁屏时同步，强制解锁会让后台同步全废
                .setUserAuthenticationRequired(false)
                .build(),
        )
        return gen.generateKey()
    }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "garmin_coach_master"
        const val TRANSFORM = "AES/GCM/NoPadding"
    }
}

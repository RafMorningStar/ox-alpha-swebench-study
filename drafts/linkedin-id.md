# Draft LinkedIn (Bahasa Indonesia)

Beberapa waktu lalu, sebuah model misterius bernama Ox Alpha muncul di OpenRouter dan OpenCode. Tidak banyak informasi tentang model di baliknya, tetapi model ini sempat ramai dibicarakan karena hasil coding-nya dan dibandingkan dengan Fable.

Saya penasaran. Daripada hanya mengikuti hasil yang beredar, saya mencoba mengujinya sendiri pada SWE-bench Verified dan membandingkannya dengan dua model yang bisa saya akses: Gemini 3.7 Flash High dan Qwen3.8-Max.

Eksperimen pertama memberi hasil yang sangat bagus untuk Ox Alpha:

- Ox Alpha: 19/20
- Qwen3.8-Max: 18/20
- Gemini: 11/20

Angkanya terlihat meyakinkan sampai saya memeriksa trajectory dari seluruh run.

Agent ternyata memiliki akses internet. Pada beberapa task, agent mencari issue, pull request, bahkan diff solusi asli di GitHub. Ketiga model memang memakai environment yang sama, tetapi mereka tidak menggunakan internet dengan frekuensi yang sama. Saya akhirnya tidak memakai hasil pertama itu sebagai kesimpulan.

Saya mengulang benchmark dengan 20 task baru. Kali ini container agent tidak memiliki akses internet. Taskset, Docker image, batas 250 model calls, timeout 45 menit, route model, retry, dan evaluator policy juga dicatat agar eksperimennya bisa diperiksa kembali.

Hasil run kedua:

- Qwen3.8-Max: 15/20
- Ox Alpha: 13/20
- Gemini 3.7 Flash High: 13/20

Hasilnya berbeda jauh dari eksperimen pertama. Namun saya juga tidak menganggap run kedua ini sebagai ranking final. Sampelnya hanya 20 task, jadi satu task saja mengubah skor lima percentage points.

Bagian yang paling banyak memakan waktu justru bukan menjalankan model. Saya harus memperbaiki model ID Gemini, menangani endpoint Ox yang gagal di lima task, mencatat perubahan routing-nya, dan mengulang evaluator setelah menemukan bahwa beberapa eval script resmi membutuhkan network untuk memasang dependency.

Saya menaruh taskset, predictions, evaluator reports, runner, metodologi, dan keterbatasannya di GitHub. Run ini saya simpan sebagai Study 01. Benchmark berikutnya akan menjadi studi baru, bukan menimpa hasil yang sudah ada.

[LINK REPOSITORY]

#LLMEvaluation #SWEBench #AIEngineering

## Notes Before Posting

- Replace `[LINK REPOSITORY]` after the repository is public.
- If you have a source for the Fable comparison, link it or name it in a comment. Otherwise keep the wording as a report of community discussion, not a verified claim.
- Attach [`../assets/study-01-results.svg`](../assets/study-01-results.svg), or export it to PNG before posting.

---
name: image-prompt
description: Make an image from a brief with a clear prompt
version: 1.0.0
tags: [creative, media, images]
requires_toolsets: [media]
---

# Image prompt

Use this skill when the user wants a picture: an illustration, a social post image, a
concept, an icon idea, or a background.

## Steps

1. Get the brief: what the image is for, where it appears, the subject, the mood, the style,
   the colors, and the shape (square, wide, or tall).
2. Write the prompt in this order: the subject and the action, the setting, the style (for
   example "flat vector illustration" or "soft natural light photo"), the composition (close
   up, wide shot, empty space on the left for text), the colors, and the mood.
3. Choose the size: `1024x1024` for square, `1536x1024` for wide, `1024x1536` for tall.
4. Call `image_generate` once. Each call costs money.
5. Ask the user what to change. Change the prompt in small steps: one or two changes for
   each new image.
6. When the image needs text (a title or a slogan), suggest adding the text later in a
   design tool, because generated text is often wrong.

## Rules

- Make one image unless the user asks for more.
- Do not make images of real people in fake situations, or images that copy the style of a
  living artist by name.
- Do not use the logos or the trademarks of others.
- Do not describe the image bytes or paste a link. Sama sends the image with your reply.
